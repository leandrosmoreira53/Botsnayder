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

Spec coverage:
  RF-01  Market discovery/selection             (main.py)
  RF-02  Book State / Market State              MarketState
  RF-03  Cost Basis Engine                      PairCostState
  RF-04  Entry Rule by simulation               _passes_pair_cost_gate
  RF-05  Bootstrap (one side zero)              _passes_pair_cost_gate + bootstrap limits
  RF-06  Lock Profit + Stop                     should_lock_profit / locked flag
  RF-07  Quote Generation (maker MM)            compute_intents / compute_quotes
  RF-08  Inventory Control (pairing)            inv_score, INV_MAX_IMBALANCE_SHARES
  RF-09  Time Phases (15m)                      Phase A/B/C/D
  RF-10  Cancel/Replace (anti-churn)            build_cancel_replace_plan + lost-top + jitter
  RF-11  Sizing                                 _compute_size
  RF-12  Observability                          log_metrics + MetricsCounters
  RNF-01 Security (post-only, fail-safe)        cross-check in compute_intents
  RNF-02 Robustness (fill dedup)               _seen_fill_ids
"""

from __future__ import annotations

import logging
import random
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

    # -- pair cost engine (RF-03, RF-04) --
    pair_cost_target: float = 0.99            # max acceptable pair_cost
    pair_cost_improvement_min: float = 0.002  # min improvement to allow an order
    lock_profit_usd: float = 0.05             # min locked profit to trigger lock (RF-06)
    min_lock_shares: float = 5.0              # min payout_floor to trigger lock (RF-06)

    # -- bootstrap (RF-05) --
    bootstrap_max_usd: float = 8.0            # max USD on first side when other is 0
    bootstrap_max_shares: float = 10.0        # max shares on first side when other is 0

    # -- sizing (RF-11) --
    bankroll: float = 100.0                   # total bankroll USD
    max_usd_per_market: float = 25.0          # max exposure per market
    max_usd_per_side: float = 12.5            # max USD on one side (YES or NO)
    min_shares: float = 5.0                   # minimum order size (shares)

    # -- quoting (RF-07) --
    edge_min: float = 0.01                    # minimum edge above mid-price
    edge_max: float = 0.05                    # maximum edge (Phase A)
    levels: int = 3                           # ladder depth (Phase A)
    level_spacing: float = 0.005              # price increment per ladder level

    # -- anti-churn (RF-10) --
    reprice_threshold: float = 0.01           # min target-price delta to trigger replace
    lost_top_ms: int = 800                    # ms before repricing when not top-of-book
    cooldown_ms: int = 500                    # ms cooldown after a replace

    # -- time phases (seconds remaining) (RF-09) --
    phase_b_start: int = 600                  # 10 min
    phase_c_start: int = 180                  # 3 min
    phase_d_start: int = 60                   # 1 min

    # -- inventory control (RF-08) --
    max_imbalance_ratio: float = 3.0          # max qty_long_side / qty_short_side
    inv_max_imbalance_shares: float = 20.0    # hard cap on |qty_yes - qty_no|

    # -- trust scoring (RF-01 quality filter) --
    trust_min_book_depth_usd: float = 50.0
    trust_max_spread: float = 0.08
    trust_min_levels: int = 2
    trust_score_threshold: float = 0.4

    # -- exposure curve --
    exposure_ratio_max: float = 0.95

    # -- mispricing intensity --
    mispricing_size_boost_max: float = 2.0
    mispricing_base_threshold: float = 0.02

    # -- accumulation mode --
    accum_imbalance_trigger: float = 1.5
    accum_cheapness_threshold: float = 0.45

    # -- discovery / main loop --
    discovery_interval_s: float = 20.0        # how often to check for new markets
    active_markets: int = 4                   # max simultaneous markets
    metrics_interval_s: float = 5.0           # log metrics every N seconds

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyConfig":
        cfg = cls()
        for k, v in d.items():
            if hasattr(cfg, k):
                expected_type = type(getattr(cfg, k))
                try:
                    setattr(cfg, k, expected_type(v))
                except (ValueError, TypeError):
                    pass
        return cfg


# ---------------------------------------------------------------------------
# Pair-Cost Engine (RF-03)
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
    def qty_imbalance_signed(self) -> float:
        """Positive = more YES than NO."""
        return self.qty_yes - self.qty_no

    @property
    def total_exposure_usd(self) -> float:
        return self.cost_yes + self.cost_no

    @property
    def is_arbitrage_locked(self) -> bool:
        """True when min(qty_yes, qty_no) > total_cost -- risk-free."""
        paired = self.payout_floor
        return paired > 0 and paired > self.total_exposure_usd

    @property
    def exposure_ratio(self) -> float:
        """cost / payout -- below 1.0 is healthy; above 1.0 means underwater."""
        if self.payout_floor <= 0:
            return float("inf")
        return self.total_exposure_usd / self.payout_floor

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
# Time phases (RF-09)
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
# Trust Score (RF-01 / quality filter)
# ---------------------------------------------------------------------------

@dataclass
class TrustScore:
    """Quality score for a market based on orderbook health."""
    spread_yes: float = 1.0
    spread_no: float = 1.0
    depth_yes_usd: float = 0.0
    depth_no_usd: float = 0.0
    levels_yes: int = 0
    levels_no: int = 0
    score: float = 0.0       # 0.0 (worst) to 1.0 (best)
    reason: str = ""


def compute_trust_score(
    yes_book: Optional[OrderBook],
    no_book: Optional[OrderBook],
    cfg: StrategyConfig,
) -> TrustScore:
    """Evaluate market quality from orderbook state."""
    ts = TrustScore()

    if yes_book is None or no_book is None:
        ts.reason = "missing_books"
        return ts

    # Spread
    ts.spread_yes = (yes_book.best_ask or 1.0) - (yes_book.best_bid or 0.0)
    ts.spread_no = (no_book.best_ask or 1.0) - (no_book.best_bid or 0.0)

    # Depth (USD value of all resting orders)
    ts.depth_yes_usd = sum(e.price * e.size for e in yes_book.bids) + \
                       sum(e.price * e.size for e in yes_book.asks)
    ts.depth_no_usd = sum(e.price * e.size for e in no_book.bids) + \
                      sum(e.price * e.size for e in no_book.asks)

    # Levels
    ts.levels_yes = min(len(yes_book.bids), len(yes_book.asks))
    ts.levels_no = min(len(no_book.bids), len(no_book.asks))

    # Score components (each 0-1)
    total_depth = ts.depth_yes_usd + ts.depth_no_usd
    depth_score = min(1.0, total_depth / (cfg.trust_min_book_depth_usd * 2))

    avg_spread = (ts.spread_yes + ts.spread_no) / 2
    spread_score = max(0.0, 1.0 - avg_spread / cfg.trust_max_spread)

    min_levels = min(ts.levels_yes, ts.levels_no)
    level_score = min(1.0, min_levels / max(cfg.trust_min_levels, 1))

    ts.score = depth_score * 0.4 + spread_score * 0.4 + level_score * 0.2

    # Hard rejects
    if total_depth < cfg.trust_min_book_depth_usd:
        ts.score = min(ts.score, 0.2)
        ts.reason = f"low_depth=${total_depth:.0f}"
    elif avg_spread > cfg.trust_max_spread:
        ts.score = min(ts.score, 0.3)
        ts.reason = f"wide_spread={avg_spread:.4f}"
    elif min_levels < cfg.trust_min_levels:
        ts.score = min(ts.score, 0.35)
        ts.reason = f"thin_levels={min_levels}"
    else:
        ts.reason = "ok"

    return ts


# ---------------------------------------------------------------------------
# Accumulation Mode (asymmetric temporal accumulation)
# ---------------------------------------------------------------------------

class AccumulationMode(Enum):
    BALANCED = "BALANCED"    # quote both sides equally
    FOCUS_YES = "FOCUS_YES"  # prioritize accumulating YES
    FOCUS_NO = "FOCUS_NO"    # prioritize accumulating NO


# ---------------------------------------------------------------------------
# Metrics Counters (RF-12)
# ---------------------------------------------------------------------------

@dataclass
class MetricsCounters:
    """Per-market operational counters, reset each metrics interval."""
    fills: int = 0
    cancels: int = 0
    replaces: int = 0
    volume_usd: float = 0.0
    last_log_ts: float = 0.0

    def record_fill(self, usd: float):
        self.fills += 1
        self.volume_usd += usd

    def record_cancel(self):
        self.cancels += 1

    def record_replace(self):
        self.replaces += 1

    def snapshot_and_reset(self, now: float) -> dict:
        elapsed = now - self.last_log_ts if self.last_log_ts > 0 else 1.0
        elapsed = max(elapsed, 0.001)
        snap = {
            "fills_per_min": self.fills / elapsed * 60,
            "cancels_per_min": self.cancels / elapsed * 60,
            "replaces_per_min": self.replaces / elapsed * 60,
            "volume_usd": self.volume_usd,
        }
        self.fills = 0
        self.cancels = 0
        self.replaces = 0
        self.volume_usd = 0.0
        self.last_log_ts = now
        return snap


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
# Per-market runtime state (RF-02)
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

    # pair cost (RF-03)
    pcs: PairCostState = field(default_factory=PairCostState)

    # live orders we manage  (order_id -> dict with token_id, side, price, size, placed_ts)
    live_orders: dict = field(default_factory=dict)

    # anti-churn (RF-10)
    last_replace_ts: float = 0.0       # epoch-ms of last cancel/replace
    last_replace_count: int = 0        # consecutive replaces (for backoff)

    # per-side cooldowns  (token_id -> epoch_ms)
    side_cooldowns: dict = field(default_factory=dict)

    # trust score (refreshed each tick)
    trust: TrustScore = field(default_factory=TrustScore)

    # accumulation mode (recomputed each tick)
    accum_mode: AccumulationMode = AccumulationMode.BALANCED

    # RF-06: lock-profit flag
    locked: bool = False

    # arbitrage lock flag (logged when first achieved)
    _arb_lock_logged: bool = False

    # RF-12: metrics
    metrics: MetricsCounters = field(default_factory=MetricsCounters)

    @property
    def secs_remaining(self) -> float:
        return max(0.0, self.end_epoch - time.time())


# ---------------------------------------------------------------------------
# Strategy (RF-01 through RF-12, RNF-01, RNF-02)
# ---------------------------------------------------------------------------

class GabagoolPairCostMMStrategy:
    """
    Maker-first pair-cost market-making scalper.

    Public interface (called by main loop):
        on_book(market_id, yes_book, no_book)       RF-02
        on_fill(market_id, side, qty, price)         RF-03, RNF-02
        tick(now) -> dict[market_id, list[OrderIntent]]  entry point per cycle
        compute_quotes(market_state) -> list[OrderIntent]  RF-07
        simulate_pair_cost(ms, side, dq, price)      RF-04
        should_lock_profit(market_state) -> bool      RF-06
        plan_requotes(ms, intents) -> (cancels, places)  RF-10
        risk_check(market_state) -> bool
    """

    def __init__(self, cfg: StrategyConfig, client: PolymarketClient):
        self.cfg = cfg
        self.client = client
        self.markets: dict[str, MarketState] = {}  # condition_id -> MarketState
        self._seen_fill_ids: set[str] = set()       # RNF-02: fill dedup

    # -- book update (RF-02) ------------------------------------------------

    def on_book(self, market_id: str, yes_book: OrderBook, no_book: OrderBook):
        ms = self.markets.get(market_id)
        if ms is None:
            return
        ms.yes_book = yes_book
        ms.no_book = no_book

        # Refresh trust score each tick
        ms.trust = compute_trust_score(yes_book, no_book, self.cfg)

        # Refresh accumulation mode
        ms.accum_mode = self._compute_accum_mode(ms)

        # Check arbitrage lock (log once)
        if ms.pcs.is_arbitrage_locked and not ms._arb_lock_logged:
            ms._arb_lock_logged = True
            log.info(
                "ARBITRAGE LOCKED [%s] profit_floor=$%.4f  exposure_ratio=%.4f  "
                "paired=%d  total_cost=$%.2f",
                market_id[:12], ms.pcs.profit_floor, ms.pcs.exposure_ratio,
                int(ms.pcs.payout_floor), ms.pcs.total_exposure_usd,
            )

    # keep backward compat alias
    on_book_update = on_book

    # -- fill handling (RF-03, RNF-02 dedup) --------------------------------

    def on_fill(
        self, market_id: str, side: str, qty: float, price: float,
        fill_id: Optional[str] = None,
    ):
        """
        Called when one of our orders is filled.
        side: "YES" or "NO" (which token was bought).
        fill_id: optional unique id for deduplication (RNF-02).
        """
        # RNF-02: deduplication
        if fill_id is not None:
            if fill_id in self._seen_fill_ids:
                log.debug("Duplicate fill ignored: %s", fill_id)
                return
            self._seen_fill_ids.add(fill_id)
            # Cap set size to prevent unbounded growth
            if len(self._seen_fill_ids) > 10_000:
                # Discard oldest half (set has no order, but this is fine)
                to_keep = list(self._seen_fill_ids)[-5_000:]
                self._seen_fill_ids = set(to_keep)

        ms = self.markets.get(market_id)
        if ms is None:
            return
        ms.pcs.apply_fill(side, qty, price)
        ms.metrics.record_fill(qty * price)
        log.info(
            "[%s] FILL side=%s qty=%.2f price=%.4f | pair_cost=%.4f payout_floor=%.2f "
            "profit_floor=%.4f arb_locked=%s",
            market_id[:12], side, qty, price,
            ms.pcs.pair_cost, ms.pcs.payout_floor, ms.pcs.profit_floor,
            ms.pcs.is_arbitrage_locked,
        )

    # -- simulate pair cost (RF-04 public interface) -------------------------

    @staticmethod
    def simulate_pair_cost(
        ms: MarketState, side: str, dq: float, price: float,
    ) -> float:
        """Simulate a buy and return the resulting pair_cost."""
        sim = ms.pcs.simulate_buy(side, dq, price)
        return sim.pair_cost

    # -- should lock profit (RF-06) -----------------------------------------

    def should_lock_profit(self, ms: MarketState) -> bool:
        """
        RF-06: True when profit_floor >= LOCK_PROFIT_USD and
        payout_floor >= MIN_LOCK_SHARES.
        """
        return (
            ms.pcs.profit_floor >= self.cfg.lock_profit_usd
            and ms.pcs.payout_floor >= self.cfg.min_lock_shares
        )

    # -- tick (main entry point per cycle) -----------------------------------

    def tick(self, now: float) -> dict[str, list[OrderIntent]]:
        """
        Called once per cycle. Returns {market_id: [OrderIntent]} for each
        active (non-locked) market.
        """
        result: dict[str, list[OrderIntent]] = {}
        for cid, ms in self.markets.items():
            if ms.locked:
                continue
            # RF-06: check lock profit
            if self.should_lock_profit(ms):
                ms.locked = True
                log.info(
                    "LOCK PROFIT [%s] profit_floor=$%.4f payout_floor=%.2f -- "
                    "cancelling all orders and stopping market",
                    cid[:12], ms.pcs.profit_floor, ms.pcs.payout_floor,
                )
                result[cid] = []  # empty = signal to cancel all
                continue
            intents = self.compute_quotes(ms)
            result[cid] = intents
        return result

    # -- trust score gate ---------------------------------------------------

    def _passes_trust_gate(self, ms: MarketState) -> bool:
        if ms.trust.score < self.cfg.trust_score_threshold:
            log.debug(
                "[%s] trust gate BLOCKED score=%.2f reason=%s",
                ms.condition_id[:12], ms.trust.score, ms.trust.reason,
            )
            return False
        return True

    # -- exposure curve gate ------------------------------------------------

    def _passes_exposure_gate(self, ms: MarketState) -> bool:
        ratio = ms.pcs.exposure_ratio
        if ratio != float("inf") and ratio > self.cfg.exposure_ratio_max:
            log.debug(
                "[%s] exposure gate BLOCKED ratio=%.4f",
                ms.condition_id[:12], ratio,
            )
            return False
        return True

    # -- mispricing intensity -----------------------------------------------

    def _mispricing_intensity(self, ms: MarketState) -> float:
        if ms.yes_book is None or ms.no_book is None:
            return 1.0
        ask_y = ms.yes_book.best_ask
        ask_n = ms.no_book.best_ask
        if ask_y is None or ask_n is None:
            return 1.0
        deviation = 1.0 - (ask_y + ask_n)
        if deviation <= self.cfg.mispricing_base_threshold:
            return 1.0
        cfg = self.cfg
        boost_range = cfg.mispricing_size_boost_max - 1.0
        normalized = min(1.0, (deviation - cfg.mispricing_base_threshold) / 0.08)
        return 1.0 + boost_range * normalized

    # -- accumulation mode --------------------------------------------------

    def _compute_accum_mode(self, ms: MarketState) -> AccumulationMode:
        pcs = ms.pcs
        cfg = self.cfg
        if pcs.qty_yes < cfg.min_shares and pcs.qty_no < cfg.min_shares:
            if ms.yes_book and ms.no_book:
                ask_y = ms.yes_book.best_ask
                ask_n = ms.no_book.best_ask
                if ask_y is not None and ask_y < cfg.accum_cheapness_threshold:
                    return AccumulationMode.FOCUS_YES
                if ask_n is not None and ask_n < cfg.accum_cheapness_threshold:
                    return AccumulationMode.FOCUS_NO
            return AccumulationMode.BALANCED
        if pcs.qty_yes > 0 and pcs.qty_no > 0:
            ratio = pcs.qty_yes / pcs.qty_no
            if ratio > cfg.accum_imbalance_trigger:
                return AccumulationMode.FOCUS_NO
            if ratio < 1.0 / cfg.accum_imbalance_trigger:
                return AccumulationMode.FOCUS_YES
        if pcs.qty_yes > 0 and pcs.qty_no == 0:
            return AccumulationMode.FOCUS_NO
        if pcs.qty_no > 0 and pcs.qty_yes == 0:
            return AccumulationMode.FOCUS_YES
        return AccumulationMode.BALANCED

    def _side_allowed_by_accum(self, ms: MarketState, token_side: str) -> bool:
        mode = ms.accum_mode
        if mode == AccumulationMode.BALANCED:
            return True
        if mode == AccumulationMode.FOCUS_YES and token_side == "YES":
            return True
        if mode == AccumulationMode.FOCUS_NO and token_side == "NO":
            return True
        return False

    # -- inventory score (RF-08) --------------------------------------------

    def _inv_score(self, ms: MarketState) -> float:
        """
        RF-08: clamp(qty_imbalance / INV_MAX_IMBALANCE, -1, +1).
        Positive means more YES than NO.
        """
        max_imb = max(self.cfg.inv_max_imbalance_shares, 1.0)
        return max(-1.0, min(1.0, ms.pcs.qty_imbalance_signed / max_imb))

    # -- core quoting (RF-07) -----------------------------------------------

    def compute_quotes(self, ms: MarketState) -> list[OrderIntent]:
        """RF-07: Generate quote intents for one market. Alias for compute_intents."""
        return self.compute_intents(ms)

    def compute_intents(self, ms: MarketState) -> list[OrderIntent]:
        """Compute desired quotes for one market. Called each tick."""
        phase = current_phase(ms.secs_remaining, self.cfg)

        if phase == Phase.D:
            return []
        if ms.yes_book is None or ms.no_book is None:
            return []
        if not self._passes_trust_gate(ms):
            return []
        if not self._passes_exposure_gate(ms):
            return []
        if ms.locked:
            return []

        arb_locked = ms.pcs.is_arbitrage_locked
        intents: list[OrderIntent] = []

        levels, size_mult, edge_mult = self._phase_params(phase)
        mispricing_mult = self._mispricing_intensity(ms)
        inv_sc = self._inv_score(ms)

        yes_mid = ms.yes_book.mid
        no_mid = ms.no_book.mid
        if yes_mid is None or no_mid is None:
            return []

        for token_side, book, token_id, mid in [
            ("YES", ms.yes_book, ms.yes_token_id, yes_mid),
            ("NO", ms.no_book, ms.no_token_id, no_mid),
        ]:
            # Accumulation mode filter
            side_focused = self._side_allowed_by_accum(ms, token_side)
            side_levels = levels if side_focused else min(1, levels)

            # Arbitrage lock: only allow the deficit side
            if arb_locked:
                if token_side == "YES" and ms.pcs.qty_yes >= ms.pcs.qty_no:
                    continue
                if token_side == "NO" and ms.pcs.qty_no >= ms.pcs.qty_yes:
                    continue

            # RF-08: skip if adding to this side would exceed imbalance hard cap
            if token_side == "YES" and ms.pcs.qty_imbalance_signed > self.cfg.inv_max_imbalance_shares:
                continue
            if token_side == "NO" and -ms.pcs.qty_imbalance_signed > self.cfg.inv_max_imbalance_shares:
                continue

            for lvl in range(side_levels):
                edge = (self.cfg.edge_min + lvl * self.cfg.level_spacing) * edge_mult

                # RF-08 asymmetry: widen edge on heavy side, tighten on light side
                if token_side == "YES" and inv_sc > 0:
                    edge *= (1.0 + inv_sc * 0.5)  # more YES → harder to buy more YES
                elif token_side == "NO" and inv_sc < 0:
                    edge *= (1.0 + abs(inv_sc) * 0.5)

                price = self._round_tick(mid - edge, ms.tick_size)

                # RNF-01: post-only safety -- never allow a buy above best ask
                best_ask = book.best_ask
                if best_ask is not None and price >= best_ask:
                    price = self._round_tick(best_ask - ms.tick_size, ms.tick_size)

                if price <= 0 or price >= 1.0:
                    continue

                size = self._compute_size(
                    ms, token_side, price, size_mult * mispricing_mult, phase,
                )
                if size < ms.min_order_size:
                    continue

                # RF-04: Simulation gate
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

    # -- phase parameters (RF-09) -------------------------------------------

    def _phase_params(self, phase: Phase) -> tuple[int, float, float]:
        """(levels, size_multiplier, edge_multiplier)"""
        if phase == Phase.A:
            return (self.cfg.levels, 1.0, 1.0)
        if phase == Phase.B:
            return (2, 0.6, 1.5)
        if phase == Phase.C:
            return (1, 0.3, 2.0)
        return (0, 0.0, 0.0)

    # -- sizing (RF-11) -----------------------------------------------------

    def _compute_size(
        self, ms: MarketState, side: str, price: float,
        size_mult: float, phase: Phase,
    ) -> float:
        cfg = self.cfg

        # Base size
        base = cfg.min_shares * size_mult

        # Budget remaining on this side
        if side == "YES":
            spent = ms.pcs.cost_yes
        else:
            spent = ms.pcs.cost_no
        side_remaining = cfg.max_usd_per_side - spent
        market_remaining = cfg.max_usd_per_market - ms.pcs.total_exposure_usd
        bankroll_remaining = cfg.bankroll - self._total_exposure_all_markets()

        budget_usd = min(side_remaining, market_remaining, bankroll_remaining)
        if budget_usd <= 0:
            return 0.0

        max_shares_budget = budget_usd / price if price > 0 else 0.0
        size = min(base, max_shares_budget)

        # Phase C: only orders that reduce imbalance
        if phase == Phase.C:
            if side == "YES" and ms.pcs.qty_yes > ms.pcs.qty_no:
                return 0.0
            if side == "NO" and ms.pcs.qty_no > ms.pcs.qty_yes:
                return 0.0

        # Inventory skew: reduce size on heavy side
        if ms.pcs.qty_yes > 0 and ms.pcs.qty_no > 0:
            if side == "YES":
                ratio = ms.pcs.qty_yes / max(ms.pcs.qty_no, 1e-9)
            else:
                ratio = ms.pcs.qty_no / max(ms.pcs.qty_yes, 1e-9)
            if ratio > 1.0:
                skew_factor = max(0.2, 1.0 / ratio)
                size *= skew_factor

        # Floor to min_order_size
        if size < ms.min_order_size:
            if max_shares_budget >= ms.min_order_size:
                size = ms.min_order_size
            else:
                return 0.0

        return round(size, 2)

    def _total_exposure_all_markets(self) -> float:
        return sum(ms.pcs.total_exposure_usd for ms in self.markets.values())

    # -- pair-cost gate (RF-04 + RF-05) --------------------------------------

    def _passes_pair_cost_gate(
        self, ms: MarketState, side: str, qty: float, price: float,
    ) -> bool:
        """
        RF-04: Simulate the buy and check:
        - RF-05: If one side is zero (bootstrap), allow with strict limits.
        - Otherwise, allow if new_pair_cost <= target OR improves by improvement_min.
        """
        pcs = ms.pcs
        sim = pcs.simulate_buy(side, qty, price)

        # RF-05: Bootstrap -- other side is still zero
        other_zero = (side == "YES" and pcs.qty_no == 0) or \
                     (side == "NO" and pcs.qty_yes == 0)

        if other_zero:
            cost_after = sim.cost_yes + sim.cost_no
            qty_this = sim.qty_yes if side == "YES" else sim.qty_no
            return (
                cost_after <= self.cfg.bootstrap_max_usd
                and qty_this <= self.cfg.bootstrap_max_shares
            )

        new_pc = sim.pair_cost
        old_pc = pcs.pair_cost

        # Accept if below target
        if new_pc <= self.cfg.pair_cost_target:
            return True

        # Accept if it improves pair_cost by at least improvement_min
        if new_pc < old_pc - self.cfg.pair_cost_improvement_min:
            return True

        return False

    # -- per-order risk check (RF-08, RF-11) ---------------------------------

    def risk_check_order(
        self, ms: MarketState, side: str, qty: float, price: float,
    ) -> bool:
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

        # Bankroll check
        if self._total_exposure_all_markets() + usd > cfg.bankroll:
            return False

        # RF-08: Max imbalance ratio
        sim = ms.pcs.simulate_buy(side, qty, price)
        if sim.qty_yes > 0 and sim.qty_no > 0:
            ratio = max(sim.qty_yes, sim.qty_no) / min(sim.qty_yes, sim.qty_no)
            if ratio > cfg.max_imbalance_ratio:
                return False

        # RF-08: Hard imbalance shares cap
        if sim.imbalance > cfg.inv_max_imbalance_shares:
            return False

        return True

    # -- aggregate risk check (per tick) ------------------------------------

    def risk_check(self, ms: MarketState) -> bool:
        if ms.locked:
            return False
        if ms.secs_remaining <= self.cfg.phase_d_start:
            return False
        if ms.pcs.total_exposure_usd > self.cfg.max_usd_per_market:
            return False
        return True

    # -- apply time phase ---------------------------------------------------

    def apply_time_phase(self, ms: MarketState) -> Phase:
        return current_phase(ms.secs_remaining, self.cfg)

    # -- cancel / replace plan (RF-10) with lost-top + jitter ---------------

    def plan_requotes(
        self,
        ms: MarketState,
        target_intents: list[OrderIntent],
    ) -> tuple[list[str], list[OrderIntent]]:
        """Alias matching spec interface name."""
        return self.build_cancel_replace_plan(ms, target_intents)

    def build_cancel_replace_plan(
        self,
        ms: MarketState,
        target_intents: list[OrderIntent],
    ) -> tuple[list[str], list[OrderIntent]]:
        """
        RF-10: Compare live_orders with target_intents.
        Returns: (cancel_ids, new_intents)
        """
        now_ms = time.time() * 1000
        cfg = self.cfg

        # Per-market cooldown with jitter
        jitter = random.uniform(0, cfg.cooldown_ms * 0.3)
        effective_cooldown = cfg.cooldown_ms + jitter

        if now_ms - ms.last_replace_ts < effective_cooldown:
            return ([], [])

        cancel_ids: list[str] = []

        # Phase D or locked: cancel everything
        phase = current_phase(ms.secs_remaining, cfg)
        if phase == Phase.D or ms.locked:
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
                used_intents.add(best_match_idx)
            else:
                # RF-10: Lost top-of-book check
                placed_ts = order.get("placed_ts", 0)
                if best_delta <= cfg.reprice_threshold * 2 and \
                   (now_ms - placed_ts) < cfg.lost_top_ms:
                    # Not yet lost long enough, keep
                    if best_match_idx is not None:
                        used_intents.add(best_match_idx)
                else:
                    cancel_ids.append(oid)

        # New intents that had no existing match
        new_intents = [
            intent for i, intent in enumerate(target_intents) if i not in used_intents
        ]

        if cancel_ids or new_intents:
            ms.last_replace_ts = now_ms
            if cancel_ids:
                ms.last_replace_count += 1
            else:
                ms.last_replace_count = 0

            # Exponential backoff on rapid consecutive replaces
            if ms.last_replace_count > 5:
                backoff_factor = min(ms.last_replace_count - 5, 5)
                ms.last_replace_ts += backoff_factor * cfg.cooldown_ms
                log.debug(
                    "[%s] anti-churn backoff: %d consecutive replaces, "
                    "added %dms cooldown",
                    ms.condition_id[:12], ms.last_replace_count,
                    backoff_factor * cfg.cooldown_ms,
                )

        for _ in cancel_ids:
            ms.metrics.record_cancel()
        if cancel_ids and new_intents:
            ms.metrics.record_replace()

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
        ms.metrics.last_log_ts = time.time()
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
                "Unregistered market %s | final pair_cost=%.4f profit_floor=%.4f "
                "locked=%s fills=%d",
                condition_id[:12], ms.pcs.pair_cost, ms.pcs.profit_floor,
                ms.locked, ms.metrics.fills,
            )

    # -- logging / metrics (RF-12) ------------------------------------------

    def log_metrics(self, ms: MarketState, force: bool = False):
        now = time.time()
        if not force and (now - ms.metrics.last_log_ts) < self.cfg.metrics_interval_s:
            return

        phase = current_phase(ms.secs_remaining, self.cfg)
        mispricing = self._mispricing_intensity(ms)
        counters = ms.metrics.snapshot_and_reset(now)

        log.info(
            "[%s] phase=%s secs_rem=%.0f accum=%s trust=%.2f(%s) locked=%s | "
            "pair_cost=%.4f avg_y=%.4f avg_n=%.4f qty_y=%.2f qty_n=%.2f "
            "imbalance=%.1f inv_score=%.2f | "
            "payout_floor=%.2f profit_floor=%.4f exp_ratio=%.4f "
            "arb_locked=%s mispricing_boost=%.2fx | "
            "exposure=$%.2f live_orders=%d | "
            "fills/min=%.1f cancels/min=%.1f replaces/min=%.1f vol=$%.2f",
            ms.condition_id[:12],
            phase.value,
            ms.secs_remaining,
            ms.accum_mode.value,
            ms.trust.score,
            ms.trust.reason,
            ms.locked,
            ms.pcs.pair_cost,
            ms.pcs.avg_yes,
            ms.pcs.avg_no,
            ms.pcs.qty_yes,
            ms.pcs.qty_no,
            ms.pcs.imbalance,
            self._inv_score(ms),
            ms.pcs.payout_floor,
            ms.pcs.profit_floor,
            ms.pcs.exposure_ratio if ms.pcs.exposure_ratio != float("inf") else -1.0,
            ms.pcs.is_arbitrage_locked,
            mispricing,
            ms.pcs.total_exposure_usd,
            len(ms.live_orders),
            counters["fills_per_min"],
            counters["cancels_per_min"],
            counters["replaces_per_min"],
            counters["volume_usd"],
        )
