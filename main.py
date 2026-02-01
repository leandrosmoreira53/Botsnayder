#!/usr/bin/env python3
"""
Gabagool22 Pair-Cost MM Scalper -- Python entry-point.

Loads config.json (same file used by the Rust bot), discovers up to 4
15-minute markets, and runs the GabagoolPairCostMMStrategy in a continuous
async loop.

Usage:
    python main.py                       # production
    python main.py --simulation          # dry-run, no real orders
    python main.py --config other.json   # custom config file
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from execution import PolymarketClient, OrderBook
from strategies.gabagool_paircost_mm import (
    GabagoolPairCostMMStrategy,
    MarketState,
    Phase,
    StrategyConfig,
    current_phase,
)

log = logging.getLogger("gabagool")

# ---------------------------------------------------------------------------
# Market discovery  (mirrors Rust main.rs discover_market)
# ---------------------------------------------------------------------------

SLUG_PREFIXES = ("eth", "btc", "sol", "doge")  # extend as needed
PERIOD_SECS = 900  # 15 minutes


def _rounded_ts(now: Optional[float] = None) -> int:
    t = int(now or time.time())
    return (t // PERIOD_SECS) * PERIOD_SECS


async def discover_markets(
    client: PolymarketClient,
    max_markets: int = 4,
) -> list[dict]:
    """
    Discover active 15-minute up/down markets.

    Returns list of dicts:
        { "condition_id", "slug", "yes_token_id", "no_token_id",
          "end_epoch", "min_order_size", "tick_size" }
    """
    now = time.time()
    rounded = _rounded_ts(now)
    found: list[dict] = []
    seen_ids: set[str] = set()

    for prefix in SLUG_PREFIXES:
        if len(found) >= max_markets:
            break
        # try current and previous 3 periods
        for offset in range(4):
            ts = rounded - offset * PERIOD_SECS
            slug = f"{prefix}-updown-15m-{ts}"
            mkt = await client.get_market_by_slug(slug)
            if mkt is None:
                continue
            cid = mkt.get("conditionId", "")
            if not cid or cid in seen_ids:
                continue
            if not mkt.get("active", False) or mkt.get("closed", False):
                continue

            # Get detailed info from CLOB
            try:
                details = await client.get_market(cid)
            except Exception as exc:
                log.warning("Could not fetch details for %s: %s", slug, exc)
                continue

            if not details.accepting_orders:
                log.info("Market %s not accepting orders, skip", slug)
                continue

            # Identify YES (Up) and NO (Down) tokens
            yes_tid = no_tid = None
            for t in details.tokens:
                outcome = t["outcome"].upper()
                if "UP" in outcome or outcome == "1":
                    yes_tid = t["token_id"]
                elif "DOWN" in outcome or outcome == "0":
                    no_tid = t["token_id"]

            if not yes_tid or not no_tid:
                log.warning("Market %s missing YES/NO tokens, skip", slug)
                continue

            # Estimate end_epoch from slug timestamp + 15 min
            end_epoch = float(ts + PERIOD_SECS)

            seen_ids.add(cid)
            found.append({
                "condition_id": cid,
                "slug": slug,
                "yes_token_id": yes_tid,
                "no_token_id": no_tid,
                "end_epoch": end_epoch,
                "min_order_size": details.minimum_order_size,
                "tick_size": details.minimum_tick_size,
            })
            log.info(
                "Discovered market: %s  cid=%s  yes=%s  no=%s  end=%s",
                slug, cid[:16], yes_tid[:16], no_tid[:16],
                datetime.fromtimestamp(end_epoch, tz=timezone.utc).isoformat(),
            )
            break  # found one for this prefix, move to next

    return found


# ---------------------------------------------------------------------------
# Fill detection (poll-based)
# ---------------------------------------------------------------------------

async def poll_fills(
    client: PolymarketClient,
    strategy: GabagoolPairCostMMStrategy,
    ms: MarketState,
):
    """
    Compare live_orders snapshot vs open_orders to detect fills.
    Simple poll approach; replace with websocket when available.
    """
    try:
        open_orders = await client.get_open_orders(market=ms.condition_id)
    except Exception:
        return

    open_ids = {o.get("id") or o.get("order_id") for o in open_orders}

    filled_ids = [oid for oid in list(ms.live_orders) if oid not in open_ids]

    for oid in filled_ids:
        order = ms.live_orders.pop(oid, None)
        if order is None:
            continue
        # Determine side: if token_id == yes_token_id -> YES, else NO
        tok_side = "YES" if order.get("token_id") == ms.yes_token_id else "NO"
        qty = float(order.get("size", 0))
        px = float(order.get("price", 0))
        if qty > 0:
            strategy.on_fill(ms.condition_id, tok_side, qty, px)


# ---------------------------------------------------------------------------
# Main loop (one tick per market)
# ---------------------------------------------------------------------------

async def tick_market(
    client: PolymarketClient,
    strategy: GabagoolPairCostMMStrategy,
    ms: MarketState,
    simulation: bool,
):
    """Execute one strategy tick for a single market."""

    # 1. Fetch orderbooks
    try:
        yes_book, no_book = await asyncio.gather(
            client.get_orderbook(ms.yes_token_id),
            client.get_orderbook(ms.no_token_id),
        )
    except Exception as exc:
        log.warning("[%s] book fetch failed: %s", ms.condition_id[:12], exc)
        return

    strategy.on_book_update(ms.condition_id, yes_book, no_book)

    # 2. Detect fills (poll-based)
    await poll_fills(client, strategy, ms)

    # 3. Log metrics
    strategy.log_metrics(ms)

    # 4. Phase check
    phase = strategy.apply_time_phase(ms)
    if phase == Phase.D:
        # Cancel everything
        if ms.live_orders and not simulation:
            await client.cancel_all()
        ms.live_orders.clear()
        return

    if not strategy.risk_check(ms):
        return

    # 5. Compute target quotes
    intents = strategy.compute_intents(ms)

    # 6. Cancel/replace plan
    cancel_ids, new_intents = strategy.build_cancel_replace_plan(ms, intents)

    # 7. Execute cancels
    if cancel_ids and not simulation:
        cancel_tasks = [client.cancel_order(oid) for oid in cancel_ids]
        await asyncio.gather(*cancel_tasks, return_exceptions=True)
    for oid in cancel_ids:
        ms.live_orders.pop(oid, None)

    # 8. Place new orders
    for intent in new_intents:
        if simulation:
            log.info(
                "[SIM] Would place %s %s %.2f @ %.4f on %s",
                intent.side, intent.label, intent.size, intent.price,
                ms.condition_id[:12],
            )
            continue

        try:
            resp = await client.place_order(
                token_id=intent.token_id,
                side=intent.side,
                size=intent.size,
                price=intent.price,
                post_only=True,
            )
            if resp.order_id:
                ms.live_orders[resp.order_id] = {
                    "token_id": intent.token_id,
                    "side": intent.side,
                    "price": intent.price,
                    "size": intent.size,
                }
                log.info(
                    "Placed %s %s %.2f @ %.4f -> %s",
                    intent.side, intent.label, intent.size, intent.price,
                    resp.order_id,
                )
        except Exception as exc:
            log.error("Order failed for %s: %s", intent.label, exc)


# ---------------------------------------------------------------------------
# Period rotation
# ---------------------------------------------------------------------------

async def rotate_markets(
    client: PolymarketClient,
    strategy: GabagoolPairCostMMStrategy,
    simulation: bool,
):
    """Check if markets expired; discover new ones."""
    now = time.time()
    expired = [
        cid for cid, ms in strategy.markets.items()
        if ms.secs_remaining <= 0
    ]

    for cid in expired:
        ms = strategy.markets[cid]
        # Cancel any remaining orders
        if ms.live_orders and not simulation:
            await client.cancel_all()
        strategy.unregister_market(cid)

    # If we have fewer than max markets, discover more
    max_markets = 4
    if len(strategy.markets) < max_markets:
        try:
            new_markets = await discover_markets(
                client, max_markets - len(strategy.markets),
            )
        except Exception as exc:
            log.warning("Discovery failed: %s", exc)
            return

        for m in new_markets:
            if m["condition_id"] in strategy.markets:
                continue
            ms = MarketState(
                condition_id=m["condition_id"],
                slug=m["slug"],
                yes_token_id=m["yes_token_id"],
                no_token_id=m["no_token_id"],
                end_epoch=m["end_epoch"],
                min_order_size=m["min_order_size"],
                tick_size=m["tick_size"],
            )
            strategy.register_market(ms)


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

async def run(config_path: str, simulation: bool):
    # Load config
    cfg_data = json.loads(Path(config_path).read_text())
    strat_cfg = StrategyConfig.from_dict(cfg_data.get("strategy", {}))

    # Override bankroll from trading.max_position_size if present
    trading_cfg = cfg_data.get("trading", {})
    if "max_position_size" in trading_cfg:
        strat_cfg.bankroll = float(trading_cfg["max_position_size"])

    client = PolymarketClient(cfg_data)
    strategy = GabagoolPairCostMMStrategy(strat_cfg, client)

    mode = "SIMULATION" if simulation else "PRODUCTION"
    log.info("Starting Gabagool22 Pair-Cost MM Scalper  [%s]", mode)
    log.info("Config: bankroll=$%.2f  pair_cost_target=%.4f  min_shares=%.0f",
             strat_cfg.bankroll, strat_cfg.pair_cost_target, strat_cfg.min_shares)

    # Initial market discovery
    await rotate_markets(client, strategy, simulation)
    if not strategy.markets:
        log.error("No markets discovered. Check network / config. Exiting.")
        await client.close()
        return

    check_interval = float(trading_cfg.get("check_interval_ms", 1000)) / 1000.0
    rotation_interval = 60.0  # check for new period every 60s
    last_rotation = time.time()

    try:
        while True:
            # Tick all active markets concurrently
            tasks = [
                tick_market(client, strategy, ms, simulation)
                for ms in list(strategy.markets.values())
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # Periodic market rotation
            if time.time() - last_rotation > rotation_interval:
                await rotate_markets(client, strategy, simulation)
                last_rotation = time.time()

            await asyncio.sleep(check_interval)

    except KeyboardInterrupt:
        log.info("Shutting down...")
    except RuntimeError as exc:
        if "401" in str(exc):
            log.error("Auth failure. Stopping. %s", exc)
        else:
            raise
    finally:
        # Cancel all on exit
        if not simulation:
            try:
                await client.cancel_all()
            except Exception:
                pass
        await client.close()

    # Final summary
    for cid, ms in strategy.markets.items():
        log.info(
            "FINAL [%s] pair_cost=%.4f profit_floor=%.4f qty_yes=%.2f qty_no=%.2f",
            cid[:12], ms.pcs.pair_cost, ms.pcs.profit_floor,
            ms.pcs.qty_yes, ms.pcs.qty_no,
        )


def main():
    parser = argparse.ArgumentParser(description="Gabagool22 Pair-Cost MM Scalper")
    parser.add_argument("--simulation", "-s", action="store_true",
                        help="Dry-run mode, no real orders")
    parser.add_argument("--config", "-c", default="config.json",
                        help="Path to config.json (default: config.json)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-12s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(args.config, args.simulation))


if __name__ == "__main__":
    main()
