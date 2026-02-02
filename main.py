#!/usr/bin/env python3
"""
Gabagool22 Pair-Cost MM Scalper -- Python entry-point.

Loads config.json (same file used by the Rust bot), discovers up to N
15-minute markets, and runs the GabagoolPairCostMMStrategy in a continuous
async loop.

Low-latency optimizations (supersnayder model):
    FASE 1: Connection pooling (TCPConnector) via execution.py
    FASE 4: SenderTask pipeline (non-blocking order submission)
    FASE 5: ReconcileTask (fill detection off hot path)
    FASE 7: uvloop on Linux (faster event loop)
    FASE 3: Conditional logging via --verbose flag

Usage:
    python main.py                       # production
    python main.py --simulation          # dry-run, no real orders
    python main.py --config other.json   # custom config file
    python main.py --verbose             # enable hot-path logging

Spec coverage:
    RF-01  Market Discovery      discover_markets + trust pre-check
    RF-06  Lock Profit + Stop    tick_market checks ms.locked
    RF-12  Observability         metrics_interval_s controlled logging
    RNF-01 Fail-safe             cancel-all on API error / shutdown
    RNF-03 Performance           concurrent book fetches + market ticks
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# FASE 7: uvloop on Linux for faster event loop
if sys.platform == "linux":
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass

# FASE 6: orjson for config loading
try:
    import orjson

    def _load_json(path: str) -> dict:
        return orjson.loads(Path(path).read_bytes())
except ImportError:
    import json as _json

    def _load_json(path: str) -> dict:
        return _json.loads(Path(path).read_text())

from execution import PolymarketClient, OrderBook
from strategies.gabagool_paircost_mm import (
    AccumulationMode,
    GabagoolPairCostMMStrategy,
    MarketState,
    MetricsCounters,
    Phase,
    StrategyConfig,
    TrustScore,
    compute_trust_score,
    current_phase,
)
from poly_data import global_state as gstate
from poly_data.sender_task import SenderTask
from poly_data.reconcile_task import ReconcileTask
from poly_data.order_intent import OrderIntentFP

log = logging.getLogger("gabagool")

# ---------------------------------------------------------------------------
# Market discovery  (RF-01)
# ---------------------------------------------------------------------------

SLUG_PREFIXES = ("eth", "btc", "sol", "doge")  # extend as needed
PERIOD_SECS = 900  # 15 minutes


def _rounded_ts(now: Optional[float] = None) -> int:
    t = int(now or time.time())
    return (t // PERIOD_SECS) * PERIOD_SECS


async def discover_markets(
    client: PolymarketClient,
    strat_cfg: StrategyConfig,
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
                if gstate._VERBOSE:
                    log.info("Market %s not accepting orders, skip", slug)
                continue

            # RF-01: time remaining filter
            end_epoch = float(ts + PERIOD_SECS)
            secs_left = end_epoch - now
            if secs_left < strat_cfg.phase_d_start:
                if gstate._VERBOSE:
                    log.info("Market %s too close to expiry (%.0fs), skip", slug, secs_left)
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

            # RF-01: Pre-check book quality (Trust Scoring)
            try:
                yb, nb = await asyncio.gather(
                    client.get_orderbook(yes_tid),
                    client.get_orderbook(no_tid),
                )
                ts_obj = compute_trust_score(yb, nb, strat_cfg)
                if ts_obj.score < 0.3:
                    if gstate._VERBOSE:
                        log.info(
                            "Market %s rejected by trust score: %.2f (%s)",
                            slug, ts_obj.score, ts_obj.reason,
                        )
                    continue
                log.info(
                    "Market %s trust=%.2f depth=$%.0f+$%.0f",
                    slug, ts_obj.score,
                    ts_obj.depth_yes_usd, ts_obj.depth_no_usd,
                )
            except Exception as exc:
                log.warning("Trust pre-check failed for %s: %s", slug, exc)

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
# Main loop (one tick per market)
# ---------------------------------------------------------------------------

async def tick_market(
    client: PolymarketClient,
    strategy: GabagoolPairCostMMStrategy,
    ms: MarketState,
    simulation: bool,
    sender: Optional[SenderTask] = None,
):
    """Execute one strategy tick for a single market.

    FASE 4: If sender is provided, order placement is non-blocking via pipeline.
    """

    # RF-06: If locked, just cancel remaining and return
    if ms.locked:
        if ms.live_orders:
            if not simulation:
                await client.cancel_all()
            ms.live_orders.clear()
        return

    # 1. Fetch orderbooks (RNF-03: concurrent)
    try:
        yes_book, no_book = await asyncio.gather(
            client.get_orderbook(ms.yes_token_id),
            client.get_orderbook(ms.no_token_id),
        )
    except Exception as exc:
        # RNF-01: fail-safe on API error
        log.warning("[%s] book fetch failed: %s -- cancelling orders",
                    ms.condition_id[:12], exc)
        if ms.live_orders and not simulation:
            try:
                await client.cancel_all()
            except Exception:
                pass
        ms.live_orders.clear()
        return

    strategy.on_book(ms.condition_id, yes_book, no_book)

    # 2. RF-12: Log metrics (respects metrics_interval_s)
    strategy.log_metrics(ms)

    # 3. RF-06: Check lock-profit after fills
    if strategy.should_lock_profit(ms):
        ms.locked = True
        log.info(
            "LOCK PROFIT [%s] profit_floor=$%.4f payout_floor=%.2f",
            ms.condition_id[:12], ms.pcs.profit_floor, ms.pcs.payout_floor,
        )
        if ms.live_orders and not simulation:
            await client.cancel_all()
        ms.live_orders.clear()
        return

    # 4. Phase check
    phase = strategy.apply_time_phase(ms)
    if phase == Phase.D:
        if ms.live_orders and not simulation:
            await client.cancel_all()
        ms.live_orders.clear()
        return

    if not strategy.risk_check(ms):
        return

    # 5. RF-07: Compute target quotes
    intents = strategy.compute_quotes(ms)

    # 6. RF-10: Cancel/replace plan
    cancel_ids, new_intents = strategy.plan_requotes(ms, intents)

    # 7. Execute cancels
    if cancel_ids and not simulation:
        if sender:
            # FASE 4: Non-blocking cancel via sender pipeline
            for oid in cancel_ids:
                sender.submit_cancel(oid)
        else:
            cancel_tasks = [client.cancel_order(oid) for oid in cancel_ids]
            await asyncio.gather(*cancel_tasks, return_exceptions=True)
    for oid in cancel_ids:
        ms.live_orders.pop(oid, None)

    # 8. Place new orders (RNF-01: always post_only=True)
    for intent in new_intents:
        # RNF-01: final cross-check -- never buy above best ask
        book = ms.yes_book if intent.token_id == ms.yes_token_id else ms.no_book
        if book and book.best_ask is not None and intent.price >= book.best_ask:
            if gstate._VERBOSE:
                log.warning(
                    "[%s] RNF-01 BLOCKED: price %.4f >= best_ask %.4f",
                    ms.condition_id[:12], intent.price, book.best_ask,
                )
            continue

        if simulation:
            if gstate._VERBOSE:
                log.info(
                    "[SIM] %s %s %.2f @ %.4f on %s",
                    intent.side, intent.label, intent.size, intent.price,
                    ms.condition_id[:12],
                )
            continue

        if sender:
            # FASE 4: Non-blocking submit to sender pipeline
            intent_fp = OrderIntentFP.from_floats(
                token_id=intent.token_id,
                side=intent.side,
                price=intent.price,
                size=intent.size,
                label=intent.label,
            )
            submitted = sender.submit(intent_fp)
            if submitted:
                # Track in live_orders (will be reconciled by ReconcileTask)
                ms.live_orders[f"pending_{intent.label}_{time.monotonic_ns()}"] = {
                    "token_id": intent.token_id,
                    "side": intent.side,
                    "price": intent.price,
                    "size": intent.size,
                    "placed_ts": time.time() * 1000,
                }
                if gstate._VERBOSE:
                    log.info(
                        "Queued %s %s %.2f @ %.4f",
                        intent.side, intent.label, intent.size, intent.price,
                    )
        else:
            # Fallback: direct placement (original behavior)
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
                        "placed_ts": time.time() * 1000,
                    }
                    if gstate._VERBOSE:
                        log.info(
                            "Placed %s %s %.2f @ %.4f -> %s",
                            intent.side, intent.label, intent.size, intent.price,
                            resp.order_id,
                        )
            except RuntimeError as exc:
                if "401" in str(exc):
                    raise
                log.error("Order failed for %s: %s", intent.label, exc)
            except Exception as exc:
                log.error("Order failed for %s: %s", intent.label, exc)


# ---------------------------------------------------------------------------
# Period rotation (RF-01)
# ---------------------------------------------------------------------------

async def rotate_markets(
    client: PolymarketClient,
    strategy: GabagoolPairCostMMStrategy,
    strat_cfg: StrategyConfig,
    simulation: bool,
):
    """Check if markets expired; discover new ones."""
    expired = [
        cid for cid, ms in strategy.markets.items()
        if ms.secs_remaining <= 0
    ]

    for cid in expired:
        ms = strategy.markets[cid]
        if ms.live_orders and not simulation:
            try:
                await client.cancel_all()
            except Exception:
                pass
        strategy.unregister_market(cid)

    max_markets = strat_cfg.active_markets
    if len(strategy.markets) < max_markets:
        try:
            new_markets = await discover_markets(
                client, strat_cfg, max_markets - len(strategy.markets),
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

async def run(config_path: str, simulation: bool, verbose: bool = False):
    # FASE 3: Set verbose flag for conditional logging
    gstate.set_verbose(verbose)

    # Load config
    cfg_data = _load_json(config_path)
    strat_cfg = StrategyConfig.from_dict(cfg_data.get("strategy", {}))

    # Override bankroll from trading.max_position_size if present
    trading_cfg = cfg_data.get("trading", {})
    if "max_position_size" in trading_cfg:
        strat_cfg.bankroll = float(trading_cfg["max_position_size"])

    client = PolymarketClient(cfg_data)
    strategy = GabagoolPairCostMMStrategy(strat_cfg, client)

    # FASE 4: Initialize sender pipeline
    sender: Optional[SenderTask] = None
    if not simulation:
        sender = SenderTask(client, batch_size=4, flush_interval_ms=50)
        gstate.SENDER = sender
        asyncio.create_task(sender.run())
        log.info("SenderTask pipeline started (batch=4, flush=50ms)")

    # FASE 5: Initialize reconcile task
    reconciler: Optional[ReconcileTask] = None
    if not simulation:
        reconciler = ReconcileTask(
            client, strategy,
            interval_s=max(2.0, strat_cfg.metrics_interval_s),
        )
        asyncio.create_task(reconciler.run())
        log.info("ReconcileTask started (interval=%.1fs)", reconciler._interval_s)

    mode = "SIMULATION" if simulation else "PRODUCTION"
    log.info("Starting Gabagool22 Pair-Cost MM Scalper  [%s]", mode)
    log.info(
        "Config: bankroll=$%.2f  pair_cost_target=%.4f  min_shares=%.0f  "
        "active_markets=%d  discovery_interval=%.0fs",
        strat_cfg.bankroll, strat_cfg.pair_cost_target, strat_cfg.min_shares,
        strat_cfg.active_markets, strat_cfg.discovery_interval_s,
    )
    log.info("Low-latency: uvloop=%s  orjson=%s  sender=%s  reconcile=%s",
             "uvloop" in sys.modules, "orjson" in sys.modules,
             sender is not None, reconciler is not None)

    # RNF-01: Never log secrets
    log.info("Auth method: %s  wallet: %s",
             cfg_data.get("polymarket", {}).get("auth_method", "?"),
             cfg_data.get("polymarket", {}).get("funder_address", "?")[:10] + "...")

    # Initial market discovery
    await rotate_markets(client, strategy, strat_cfg, simulation)
    if not strategy.markets:
        log.error("No markets discovered. Check network / config. Exiting.")
        await client.close()
        return

    check_interval = float(trading_cfg.get("check_interval_ms", 1000)) / 1000.0
    rotation_interval = strat_cfg.discovery_interval_s
    last_rotation = time.time()

    try:
        while True:
            # Tick all active markets concurrently (RNF-03)
            tasks = [
                tick_market(client, strategy, ms, simulation, sender)
                for ms in list(strategy.markets.values())
            ]
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                # RNF-01: check for auth failures
                for r in results:
                    if isinstance(r, RuntimeError) and "401" in str(r):
                        log.error("Auth failure detected. Stopping. %s", r)
                        raise r

            # Periodic market rotation
            if time.time() - last_rotation > rotation_interval:
                await rotate_markets(client, strategy, strat_cfg, simulation)
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
        # Graceful shutdown
        if sender:
            await sender.stop()
        if reconciler:
            await reconciler.stop()
        # RNF-01: Cancel all on exit (fail-safe)
        if not simulation:
            try:
                await client.cancel_all()
            except Exception:
                pass
        await client.close()

    # Final summary per market
    for cid, ms in strategy.markets.items():
        log.info(
            "FINAL [%s] pair_cost=%.4f profit_floor=%.4f qty_yes=%.2f qty_no=%.2f "
            "locked=%s",
            cid[:12], ms.pcs.pair_cost, ms.pcs.profit_floor,
            ms.pcs.qty_yes, ms.pcs.qty_no, ms.locked,
        )
    if sender:
        log.info("Sender metrics: %s", sender.metrics)


def main():
    parser = argparse.ArgumentParser(description="Gabagool22 Pair-Cost MM Scalper")
    parser.add_argument("--simulation", "-s", action="store_true",
                        help="Dry-run mode, no real orders")
    parser.add_argument("--config", "-c", default="config.json",
                        help="Path to config.json (default: config.json)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable verbose hot-path logging (FASE 3)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-12s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(args.config, args.simulation, args.verbose))


if __name__ == "__main__":
    main()
