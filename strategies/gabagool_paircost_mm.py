"""
Gabagool22 Pair-Cost MM Scalper
===============================

Maker-first market-making strategy for Polymarket 15-minute binary markets.

Core idea (from "Inside the Mind of a Polymarket Bot"):
  - Buy YES *and* NO tokens so that avg_yes + avg_no < $1.00 (pair_cost < 1).
  - Every paired unit locks in (1 - pair_cost) profit at settlement.
  - Use asymmetric quoting to keep qty_yes ~= qty_no.
  - Time-phase risk: tighten spreads and flatten as expiry approaches.

NON-DIRECTIONAL. All orders are post-only (maker). No market orders by default.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from execution import OrderBook, PolymarketClient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (loaded from config.json "strategy" section)
# ---------------------------------------------------------------------------

@dataclass
class StrategyConfig:
    """All tuning knobs. Loaded once from config.json['strategy']."""

    # -- pair cost engine --
    pair_cost_target: float = 0.99       # max acceptable pair_cost
    lock_profit_usd: float = 0.005       # min locked profit per paired unit
    min_lock_shares: float = 5.0         # min shares to consider locking

    # -- sizing --
    bankroll: float = 100.0              # total bankroll USD
    max_usd_per_market: float = 25.0     # max exposure per market
    max_usd_per_side: float = 15.0       # max USD on one side (YES or NO)
    min_shares: float = 5.0              # minimum order size (shares)

    # -- quoting --
    edge_min: float = 0.01              # minimum edge above mid-price
    edge_max: float = 0.05              # maximum edge (Phase A)
    levels: int = 3                     # ladder depth (Phase A)
    level_spacing: float = 0.005        # price increment per ladder level

    # -- anti-churn --
    reprice_threshold: float = 0.005     # min target-price delta to trigger replace
    lost_top_ms: int = 3000              # ms before repricing when not top-of-book
    cooldown_ms: int = 500               # ms cooldown after a replace

    # -- time phases (seconds remaining) --
    phase_b_start: int = 600             # 10 min
    phase_c_start: int = 180             # 3 min
    phase_d_start: int = 60              # 1 min

    # -- misc --
    max_imbalance_ratio: float = 3.0     # max qty_long_side / qty_short_side
    unpaired_max_usd: float = 10.0       # max USD exposure before other side acquired

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyConfig":
        cfg = cls()
        for k, v in d.items():
            if hasattr(cfg, k):
                setattr(cfg, k, type(getattr(cfg, k))(v))
        return cfg


# ---------------------------------------------------------------------------
# Pair-Cost Engine
# ---------------------------------------------------------------------------

@dataclass
class PairCostState:
    """Per-market cost-basis tracker."""

    qty_yes: float = 0.0
    cost_yes: float = 0.0   # total USD spent on YES
    qty_no: float = 0.0
    cost_no: float = 0.0    # total USD spent on NO

    @property
    def avg_yes(self) -> float:
        return self.cost_yes / self.qty_yes if self.qty_yes > 0 else 0.0

    @property
    def avg_no(self) -> float:
        return self.cost_no / self.qty_no if self.qty_no > 0 else 0.0

    @property
    def pair_cost(self) -> float:
        if self.qty_yes > 0 and self.qty_no > 0:
            return self.avg_yes + self.avg_no
        return float("inf")

    @property
    def payout_floor(self) -> float:
        return min(self.qty_yes, self.qty_no)

    @property
    def profit_floor(self) -> float:
        """Locked profit at settlement for paired units."""
        paired = self.payout_floor
        if paired <= 0:
            return 0.0
        return paired * 1.0 - (self.cost_yes + self.cost_no)

    @property
    def imbalance(self) -> float:
        return abs(self.qty_yes - self.qty_no)

    @property
    def total_exposure_usd(self) -> float:
        return self.cost_yes + self.cost_no

    def simulate_buy(self, side: str, qty: float, price: float) -> "PairCostState":
        """Return a *copy* with the hypothetical fill applied."""
        s = PairCostState(
            qty_yes=self.qty_yes,
            cost_yes=self.cost_yes,
            qty_no=self.qty_no,
            cost_no=self.cost_no,
        )
        if side == "YES":
            s.qty_yes += qty
            s.cost_yes += qty * price
        else:
            s.qty_no += qty
            s.cost_no += qty * price
        return s

    def apply_fill(self, side: str, qty: float, price: float):
        """Mutate state after a confirmed fill."""
        if side == "YES":
            self.qty_yes += qty
            self.cost_yes += qty * price
        else:
            self.qty_no += qty
            self.cost_no += qty * price


# ---------------------------------------------------------------------------
# Time phases
# ---------------------------------------------------------------------------

class Phase(Enum):
    A = "A"   # normal MM
    B = "B"   # tighten + reduce sizing
    C = "C"   # flatten mode
    D = "D"   # cancel-all, no new risk


def current_phase(secs_remaining: float, cfg: StrategyConfig) -> Phase:
    if secs_remaining <= cfg.phase_d_start:
        return Phase.D
    if secs_remaining <= cfg.phase_c_start:
        return Phase.C
    if secs_remaining <= cfg.phase_b_start:
        return Phase.B
    return Phase.A


# ---------------------------------------------------------------------------
# Order intent (strategy output)
# ---------------------------------------------------------------------------

@dataclass
class OrderIntent:
    token_id: str
    side: str        # "BUY" or "SELL"
    price: float
    size: float
    label: str = ""  # e.g. "YES-L1", "NO-L2"  (for logging only)


# ---------------------------------------------------------------------------
# Per-market runtime state
# ---------------------------------------------------------------------------

@dataclass
class MarketState:
    condition_id: str
    slug: str
    yes_token_id: str
    no_token_id: str
    end_epoch: float              # unix-seconds when market settles
    min_order_size: float = 5.0
    tick_size: float = 0.01

    # live data (refreshed each tick)
    yes_book: Optional[OrderBook] = None
    no_book: Optional[OrderBook] = None

    # pair cost
    pcs: PairCostState = field(default_factory=PairCostState)

    # live orders we manage  (order_id -> dict with token_id, side, price, size)
    live_orders: dict = field(default_factory=dict)

    # anti-churn
    last_replace_ts: float = 0.0  # epoch-ms of last cancel/replace

    @property
    def secs_remaining(self) -> float:
        return max(0.0, self.end_epoch - time.time())


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class GabagoolPairCostMMStrategy:
    """
    Maker-first pair-cost market-making scalper.

    Public interface (called by main loop):
        on_book_update(market_id, book_state)
        on_fill(market_id, fill_event)
        compute_intents(market_state) -> list[OrderIntent]
        risk_check(market_state) -> bool
        apply_time_phase(market_state)
        build_cancel_replace_plan(live_orders, target_quotes)
    """

    def __init__(self, cfg: StrategyConfig, client: PolymarketClient):
        self.cfg = cfg
        self.client = client
        self.markets: dict[str, MarketState] = {}  # condition_id -> MarketState

    # -- book update --------------------------------------------------------

    def on_book_update(self, market_id: str, yes_book: OrderBook, no_book: OrderBook):
        ms = self.markets.get(market_id)
        if ms is None:
            return
        ms.yes_book = yes_book
        ms.no_book = no_book

    # -- fill handling ------------------------------------------------------

    def on_fill(self, market_id: str, side: str, qty: float, price: float):
        """
        Called when one of our orders is filled.
        side: "YES" or "NO" (which token was bought).
        """
        ms = self.markets.get(market_id)
        if ms is None:
            return
        ms.pcs.apply_fill(side, qty, price)
        log.info(
            "[%s] FILL side=%s qty=%.2f price=%.4f | pair_cost=%.4f payout_floor=%.2f profit_floor=%.4f",
            market_id[:12], side, qty, price,
            ms.pcs.pair_cost, ms.pcs.payout_floor, ms.pcs.profit_floor,
        )

    # -- core quoting -------------------------------------------------------

    def compute_intents(self, ms: MarketState) -> list[OrderIntent]:
        """Compute desired quotes for one market. Called each tick."""
        phase = current_phase(ms.secs_remaining, self.cfg)

        # Phase D: no new orders
        if phase == Phase.D:
            return []

        if ms.yes_book is None or ms.no_book is None:
            return []

        intents: list[OrderIntent] = []

        # Determine levels and sizing multiplier per phase
        levels, size_mult, edge_mult = self._phase_params(phase)

        yes_mid = ms.yes_book.mid
        no_mid = ms.no_book.mid
        if yes_mid is None or no_mid is None:
            return []

        # For each side (YES, NO), build a bid ladder
        for token_side, book, token_id, mid in [
            ("YES", ms.yes_book, ms.yes_token_id, yes_mid),
            ("NO", ms.no_book, ms.no_token_id, no_mid),
        ]:
            for lvl in range(levels):
                edge = (self.cfg.edge_min + lvl * self.cfg.level_spacing) * edge_mult
                price = self._round_tick(mid - edge, ms.tick_size)
                if price <= 0 or price >= 1.0:
                    continue

                size = self._compute_size(ms, token_side, price, size_mult, phase)
                if size < ms.min_order_size:
                    continue

                # Simulation gate: only place if it improves pair_cost or we
                # still need to acquire the other side
                if not self._passes_pair_cost_gate(ms, token_side, size, price):
                    continue

                # Risk check
                if not self.risk_check_order(ms, token_side, size, price):
                    continue

                intents.append(OrderIntent(
                    token_id=token_id,
                    side="BUY",
                    price=price,
                    size=size,
                    label=f"{token_side}-L{lvl+1}",
                ))

        return intents

    # -- phase parameters ---------------------------------------------------

    def _phase_params(self, phase: Phase) -> tuple[int, float, float]:
        """(levels, size_multiplier, edge_multiplier)"""
        if phase == Phase.A:
            return (self.cfg.levels, 1.0, 1.0)
        if phase == Phase.B:
            return (2, 0.6, 1.5)       # fewer levels, smaller size, wider edge
        if phase == Phase.C:
            return (1, 0.3, 2.0)        # 1 level, tiny size, wide edge
        return (0, 0.0, 0.0)           # Phase D: no orders

    # -- sizing -------------------------------------------------------------

    def _compute_size(
        self, ms: MarketState, side: str, price: float,
        size_mult: float, phase: Phase,
    ) -> float:
        """Determine order size in shares, respecting all limits."""
        cfg = self.cfg

        # Base size: min_shares scaled by multiplier
        base = cfg.min_shares * size_mult

        # Budget remaining on this side
        if side == "YES":
            spent = ms.pcs.cost_yes
        else:
            spent = ms.pcs.cost_no
        side_remaining = cfg.max_usd_per_side - spent
        market_remaining = cfg.max_usd_per_market - ms.pcs.total_exposure_usd

        budget_usd = min(side_remaining, market_remaining)
        if budget_usd <= 0:
            return 0.0

        max_shares_budget = budget_usd / price if price > 0 else 0.0
        size = min(base, max_shares_budget)

        # In Phase C only allow orders that reduce imbalance
        if phase == Phase.C:
            if side == "YES" and ms.pcs.qty_yes > ms.pcs.qty_no:
                return 0.0
            if side == "NO" and ms.pcs.qty_no > ms.pcs.qty_yes:
                return 0.0

        # Inventory skew: if one side is much larger, reduce its size
        if ms.pcs.qty_yes > 0 and ms.pcs.qty_no > 0:
            if side == "YES":
                ratio = ms.pcs.qty_yes / max(ms.pcs.qty_no, 1e-9)
            else:
                ratio = ms.pcs.qty_no / max(ms.pcs.qty_yes, 1e-9)
            if ratio > 1.0:
                skew_factor = max(0.2, 1.0 / ratio)
                size *= skew_factor

        # Ensure we don't go below min_order_size (the exchange minimum)
        if size < ms.min_order_size:
            if max_shares_budget >= ms.min_order_size:
                size = ms.min_order_size
            else:
                return 0.0

        return round(size, 2)

    # -- pair-cost gate (Rule 4) --------------------------------------------

    def _passes_pair_cost_gate(
        self, ms: MarketState, side: str, qty: float, price: float,
    ) -> bool:
        """
        Simulate the buy and check:
        - If one side is zero, allow with strict risk limits.
        - Otherwise, only allow if new_pair_cost <= target OR improves pair_cost.
        """
        pcs = ms.pcs
        sim = pcs.simulate_buy(side, qty, price)

        # If the *other* side is still zero we allow (need to build a pair)
        other_zero = (side == "YES" and pcs.qty_no == 0) or \
                     (side == "NO" and pcs.qty_yes == 0)

        if other_zero:
            # Strict unpaired risk limit
            cost_after = sim.cost_yes + sim.cost_no
            return cost_after <= self.cfg.unpaired_max_usd

        new_pc = sim.pair_cost
        old_pc = pcs.pair_cost

        # Accept if below target
        if new_pc <= self.cfg.pair_cost_target:
            return True

        # Accept if it meaningfully improves pair_cost (by at least 0.001)
        if new_pc < old_pc - 0.001:
            return True

        return False

    # -- per-order risk check -----------------------------------------------

    def risk_check_order(
        self, ms: MarketState, side: str, qty: float, price: float,
    ) -> bool:
        """Hard limits that block an order regardless of pair-cost."""
        cfg = self.cfg
        usd = qty * price

        # Max USD per side
        if side == "YES":
            if ms.pcs.cost_yes + usd > cfg.max_usd_per_side:
                return False
        else:
            if ms.pcs.cost_no + usd > cfg.max_usd_per_side:
                return False

        # Max USD per market
        if ms.pcs.total_exposure_usd + usd > cfg.max_usd_per_market:
            return False

        # Max imbalance ratio
        sim = ms.pcs.simulate_buy(side, qty, price)
        if sim.qty_yes > 0 and sim.qty_no > 0:
            ratio = max(sim.qty_yes, sim.qty_no) / min(sim.qty_yes, sim.qty_no)
            if ratio > cfg.max_imbalance_ratio:
                return False

        return True

    # -- aggregate risk check (per tick) ------------------------------------

    def risk_check(self, ms: MarketState) -> bool:
        """Return True if the market is in a healthy state for quoting."""
        if ms.secs_remaining <= self.cfg.phase_d_start:
            return False
        if ms.pcs.total_exposure_usd > self.cfg.max_usd_per_market:
            return False
        return True

    # -- apply time phase (mutates nothing, used for logging) ---------------

    def apply_time_phase(self, ms: MarketState) -> Phase:
        phase = current_phase(ms.secs_remaining, self.cfg)
        return phase

    # -- cancel / replace plan (anti-churn, Rule 7) -------------------------

    def build_cancel_replace_plan(
        self,
        ms: MarketState,
        target_intents: list[OrderIntent],
    ) -> tuple[list[str], list[OrderIntent]]:
        """
        Compare live_orders with target_intents.

        Returns:
            (cancel_ids, new_intents)
        """
        now_ms = time.time() * 1000
        cfg = self.cfg

        # Cooldown guard
        if now_ms - ms.last_replace_ts < cfg.cooldown_ms:
            return ([], [])

        cancel_ids: list[str] = []
        keep_order_ids: set[str] = set()

        # Phase D: cancel everything
        phase = current_phase(ms.secs_remaining, cfg)
        if phase == Phase.D:
            cancel_ids = list(ms.live_orders.keys())
            return (cancel_ids, [])

        # Match existing orders to target intents
        used_intents: set[int] = set()

        for oid, order in ms.live_orders.items():
            best_match_idx: Optional[int] = None
            best_delta: float = float("inf")

            for i, intent in enumerate(target_intents):
                if i in used_intents:
                    continue
                if intent.token_id != order.get("token_id"):
                    continue
                if intent.side != order.get("side"):
                    continue
                delta = abs(intent.price - float(order.get("price", 0)))
                if delta < best_delta:
                    best_delta = delta
                    best_match_idx = i

            if best_match_idx is not None and best_delta <= cfg.reprice_threshold:
                # Close enough -- keep the existing order
                keep_order_ids.add(oid)
                used_intents.add(best_match_idx)
            else:
                # Target moved too far -- cancel
                cancel_ids.append(oid)

        # New intents that had no existing match
        new_intents = [
            intent for i, intent in enumerate(target_intents) if i not in used_intents
        ]

        if cancel_ids or new_intents:
            ms.last_replace_ts = now_ms

        return (cancel_ids, new_intents)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _round_tick(price: float, tick: float) -> float:
        if tick <= 0:
            tick = 0.01
        return round(round(price / tick) * tick, 4)

    # -- market lifecycle ---------------------------------------------------

    def register_market(self, ms: MarketState):
        self.markets[ms.condition_id] = ms
        log.info(
            "Registered market %s (%s) yes=%s no=%s end=%.0f",
            ms.condition_id[:12], ms.slug,
            ms.yes_token_id[:12], ms.no_token_id[:12],
            ms.end_epoch,
        )

    def unregister_market(self, condition_id: str):
        ms = self.markets.pop(condition_id, None)
        if ms:
            log.info(
                "Unregistered market %s | final pair_cost=%.4f profit_floor=%.4f",
                condition_id[:12], ms.pcs.pair_cost, ms.pcs.profit_floor,
            )

    # -- logging / metrics --------------------------------------------------

    def log_metrics(self, ms: MarketState):
        phase = current_phase(ms.secs_remaining, self.cfg)
        log.info(
            "[%s] phase=%s secs_rem=%.0f | pair_cost=%.4f avg_y=%.4f avg_n=%.4f "
            "qty_y=%.2f qty_n=%.2f | payout_floor=%.2f profit_floor=%.4f "
            "exposure=$%.2f live_orders=%d",
            ms.condition_id[:12],
            phase.value,
            ms.secs_remaining,
            ms.pcs.pair_cost,
            ms.pcs.avg_yes,
            ms.pcs.avg_no,
            ms.pcs.qty_yes,
            ms.pcs.qty_no,
            ms.pcs.payout_floor,
            ms.pcs.profit_floor,
            ms.pcs.total_exposure_usd,
            len(ms.live_orders),
        )
