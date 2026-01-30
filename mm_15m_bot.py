#!/usr/bin/env python3
"""
mm_15m_bot.py — Bot Market-Maker de 15 minutos para Polymarket (Caminho 2).

Fases:
  A  0–10 min  MM_NORMAL      post-only, quotes bid/ask em YES e NO
  B 10–12 min  NO_NEW_ENTRIES  sem novas posições, reduz inventário
  C 12–13 min  TIGHTEN_REDUCE  spread apertado, tamanho mínimo
  D 13–14 min  FLATTEN         zeragem (postOnly=false se necessário)
  E 14–15 min  DO_NOT_TRADE    sem ordens, validação final

Uso:
  python mm_15m_bot.py              # usa .env do diretório
  python mm_15m_bot.py --dry-run    # override DRY_RUN=true
"""

import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OpenOrderParams,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mm_15m")

# ---------------------------------------------------------------------------
# Enums / constants
# ---------------------------------------------------------------------------

class Phase(str, Enum):
    MM_NORMAL = "A_MM_NORMAL"          # 0–10 min
    NO_NEW_ENTRIES = "B_NO_NEW_ENTRIES" # 10–12 min
    TIGHTEN_REDUCE = "C_TIGHTEN"       # 12–13 min
    FLATTEN = "D_FLATTEN"              # 13–14 min
    DO_NOT_TRADE = "E_STOP"            # 14–15 min

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

# ---------------------------------------------------------------------------
# Config from .env
# ---------------------------------------------------------------------------

@dataclass
class BotConfig:
    # Auth
    host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    private_key: str = ""
    signature_type: int = 1
    funder: str = ""

    # Market
    token_id_yes: str = ""
    token_id_no: str = ""
    market_end_ts: int = 0  # unix epoch UTC
    tick_size: str = "0.01"
    min_order_size: float = 5.0

    # Bot params
    dry_run: bool = True
    base_order_size: float = 10.0
    max_position_shares: float = 100.0
    spread_normal: float = 0.04    # 4 cents
    spread_tight: float = 0.02     # 2 cents
    spread_flatten: float = 0.01   # 1 cent — agressivo
    amend_threshold: float = 0.005 # diferença mínima p/ recotar
    loop_interval_ms: int = 3000
    max_daily_loss_usd: float = 0.0  # 0 = sem limite
    max_balance_utilization: float = 0.0  # 0 = sem limite

    @classmethod
    def from_env(cls) -> "BotConfig":
        load_dotenv()
        cfg = cls()
        cfg.host = os.getenv("POLYMARKET_HOST", cfg.host)
        cfg.chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", str(cfg.chain_id)))
        cfg.private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        cfg.signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
        cfg.funder = os.getenv("POLYMARKET_FUNDER", "")

        cfg.token_id_yes = os.getenv("TOKEN_ID_YES", "")
        cfg.token_id_no = os.getenv("TOKEN_ID_NO", "")
        cfg.market_end_ts = int(os.getenv("MARKET_END_TS", "0"))
        cfg.tick_size = os.getenv("TICK_SIZE", "0.01")
        cfg.min_order_size = float(os.getenv("MIN_ORDER_SIZE", "5"))

        cfg.dry_run = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
        cfg.base_order_size = float(os.getenv("BASE_ORDER_SIZE", "10"))
        cfg.max_position_shares = float(os.getenv("MAX_POSITION_SHARES", "100"))
        cfg.spread_normal = float(os.getenv("SPREAD_NORMAL", "0.04"))
        cfg.spread_tight = float(os.getenv("SPREAD_TIGHT", "0.02"))
        cfg.spread_flatten = float(os.getenv("SPREAD_FLATTEN", "0.01"))
        cfg.amend_threshold = float(os.getenv("AMEND_THRESHOLD", "0.005"))
        cfg.loop_interval_ms = int(os.getenv("LOOP_INTERVAL_MS", "3000"))
        cfg.max_daily_loss_usd = float(os.getenv("MAX_DAILY_LOSS_USD", "0"))
        cfg.max_balance_utilization = float(os.getenv("MAX_BALANCE_UTILIZATION", "0"))
        return cfg

    @property
    def market_start_ts(self) -> int:
        return self.market_end_ts - 900

    @property
    def order_size(self) -> float:
        return max(self.min_order_size, self.base_order_size)

    def validate(self):
        errors = []
        if not self.token_id_yes:
            errors.append("TOKEN_ID_YES não configurado")
        if not self.token_id_no:
            errors.append("TOKEN_ID_NO não configurado")
        if self.market_end_ts == 0:
            errors.append("MARKET_END_TS não configurado")
        if not self.private_key and not self.dry_run:
            errors.append("POLYMARKET_PRIVATE_KEY necessário para modo real")
        if errors:
            for e in errors:
                log.error(e)
            sys.exit(1)


# ---------------------------------------------------------------------------
# Inventory tracker (local)
# ---------------------------------------------------------------------------

@dataclass
class Inventory:
    """Rastreia posição local a partir de fills estimados."""
    yes_shares: float = 0.0
    no_shares: float = 0.0

    @property
    def net_yes(self) -> float:
        return self.yes_shares

    @property
    def net_no(self) -> float:
        return self.no_shares

    @property
    def is_flat(self) -> bool:
        return abs(self.yes_shares) < 0.5 and abs(self.no_shares) < 0.5

    def update_from_fill(self, token_label: str, side: str, size: float):
        """Atualiza inventário baseado em fill."""
        if token_label == "YES":
            if side == SIDE_BUY:
                self.yes_shares += size
            else:
                self.yes_shares -= size
        elif token_label == "NO":
            if side == SIDE_BUY:
                self.no_shares += size
            else:
                self.no_shares -= size


# ---------------------------------------------------------------------------
# CLOB client wrapper
# ---------------------------------------------------------------------------

class MarketMaker:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.inventory = Inventory()
        self.open_order_ids: list[str] = []
        self._consecutive_errors = 0
        self._max_consecutive_errors = 10
        self._last_mid_yes: float | None = None
        self._last_mid_no: float | None = None

        # Cliente CLOB (sem auth para dry-run, com auth para real)
        if cfg.dry_run:
            self.client = ClobClient(cfg.host, chain_id=cfg.chain_id)
            log.info("DRY_RUN ativado — nenhuma ordem será enviada")
        else:
            self.client = ClobClient(
                cfg.host,
                key=cfg.private_key,
                chain_id=cfg.chain_id,
                signature_type=cfg.signature_type,
                funder=cfg.funder if cfg.funder else None,
            )
            # Derivar API creds para level 2 (cancel, get_orders, etc.)
            try:
                self.client.set_api_creds(self.client.create_or_derive_api_creds())
                log.info("API credentials derivadas com sucesso (L2 auth)")
            except Exception as exc:
                log.error(f"Falha ao derivar API creds: {exc}")
                sys.exit(1)

    # ------------------------------------------------------------------
    # Phase resolution
    # ------------------------------------------------------------------
    def current_phase(self) -> Phase:
        now = int(time.time())
        elapsed = now - self.cfg.market_start_ts
        if elapsed < 0:
            # Antes do início — aguardar
            return Phase.DO_NOT_TRADE
        minutes = elapsed / 60.0
        if minutes < 10:
            return Phase.MM_NORMAL
        elif minutes < 12:
            return Phase.NO_NEW_ENTRIES
        elif minutes < 13:
            return Phase.TIGHTEN_REDUCE
        elif minutes < 14:
            return Phase.FLATTEN
        else:
            return Phase.DO_NOT_TRADE

    def time_remaining_s(self) -> float:
        return max(0.0, self.cfg.market_end_ts - time.time())

    def minutes_elapsed(self) -> float:
        return (time.time() - self.cfg.market_start_ts) / 60.0

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def get_midpoint(self, token_id: str) -> float | None:
        """Retorna midpoint do CLOB. None se sem book."""
        try:
            mid = self.client.get_midpoint(token_id)
            if mid is not None:
                return float(mid)
        except Exception as exc:
            log.warning(f"Erro ao obter midpoint ({token_id[:12]}...): {exc}")
        return None

    def get_book_summary(self, token_id: str) -> dict:
        """Retorna resumo do book: best_bid, best_ask, min_order_size, tick_size."""
        try:
            book = self.client.get_order_book(token_id)
            return {
                "best_bid": float(book.bids[0].price) if book.bids else None,
                "best_ask": float(book.asks[0].price) if book.asks else None,
                "min_order_size": float(book.min_order_size) if book.min_order_size else self.cfg.min_order_size,
                "tick_size": book.tick_size or self.cfg.tick_size,
            }
        except Exception as exc:
            log.warning(f"Erro ao obter book ({token_id[:12]}...): {exc}")
            return {
                "best_bid": None,
                "best_ask": None,
                "min_order_size": self.cfg.min_order_size,
                "tick_size": self.cfg.tick_size,
            }

    def refresh_midpoints(self):
        """Atualiza midpoints para ambos tokens."""
        self._last_mid_yes = self.get_midpoint(self.cfg.token_id_yes)
        self._last_mid_no = self.get_midpoint(self.cfg.token_id_no)
        log.info(f"Mid YES={self._last_mid_yes}, Mid NO={self._last_mid_no}")

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------
    def _round_price(self, price: float) -> float:
        """Arredonda preço para o tick_size configurado."""
        tick = float(self.cfg.tick_size)
        rounded = round(round(price / tick) * tick, 4)
        # Clamp entre 0.01 e 0.99 (preços válidos Polymarket)
        return max(0.01, min(0.99, rounded))

    def place_quote(self, token_id: str, side: str, price: float, size: float,
                    post_only: bool = True) -> str | None:
        """
        Coloca uma ordem. Retorna order_id ou None.
        Em DRY_RUN, apenas loga.
        """
        price = self._round_price(price)
        size = round(size, 2)

        # Validações
        if size < self.cfg.min_order_size:
            size = self.cfg.min_order_size
        if price <= 0 or price >= 1.0:
            log.warning(f"Preço inválido {price}, pulando ordem")
            return None

        token_label = "YES" if token_id == self.cfg.token_id_yes else "NO"
        log.info(f"  {'[DRY]' if self.cfg.dry_run else '[LIVE]'} "
                 f"{side} {size} {token_label} @ {price} "
                 f"(post_only={post_only})")

        if self.cfg.dry_run:
            return f"dry-{token_label}-{side}-{price}"

        try:
            # Verificar neg_risk
            neg_risk = False
            try:
                neg_risk = self.client.get_neg_risk(self.cfg.token_id_yes)
            except Exception:
                pass

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )
            options = PartialCreateOrderOptions(
                tick_size=self.cfg.tick_size,
                neg_risk=neg_risk,
            )
            signed_order = self.client.create_order(order_args, options)
            resp = self.client.post_order(signed_order, orderType=OrderType.GTC, post_only=post_only)

            order_id = None
            if isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
                status = resp.get("status", "?")
                if status == "matched" or status == "live":
                    log.info(f"    Ordem aceita: {order_id} (status={status})")
                    if order_id:
                        self.open_order_ids.append(order_id)
                    # Estimar fill se matched
                    if status == "matched":
                        self.inventory.update_from_fill(token_label, side, size)
                else:
                    log.warning(f"    Ordem resposta inesperada: {resp}")
            else:
                log.warning(f"    Resposta não-dict: {resp}")

            self._consecutive_errors = 0
            return order_id

        except Exception as exc:
            self._consecutive_errors += 1
            log.error(f"    Erro ao enviar ordem: {exc}")
            self._check_kill_switch()
            return None

    def cancel_order(self, order_id: str) -> bool:
        if self.cfg.dry_run:
            log.info(f"  [DRY] Cancel order {order_id}")
            return True
        try:
            self.client.cancel(order_id)
            if order_id in self.open_order_ids:
                self.open_order_ids.remove(order_id)
            return True
        except Exception as exc:
            log.warning(f"  Erro ao cancelar {order_id}: {exc}")
            return False

    def cancel_all(self):
        """Cancela todas as ordens abertas."""
        if self.cfg.dry_run:
            log.info("  [DRY] Cancel ALL orders")
            self.open_order_ids.clear()
            return
        try:
            self.client.cancel_all()
            self.open_order_ids.clear()
            log.info("  Todas as ordens canceladas")
        except Exception as exc:
            log.warning(f"  Erro ao cancelar todas: {exc}")

    def get_open_orders(self) -> list[dict]:
        """Lista ordens abertas via API."""
        if self.cfg.dry_run:
            return []
        try:
            resp = self.client.get_orders(
                OpenOrderParams(asset_id=self.cfg.token_id_yes)
            )
            orders_yes = resp if isinstance(resp, list) else resp.get("orders", []) if isinstance(resp, dict) else []
            resp2 = self.client.get_orders(
                OpenOrderParams(asset_id=self.cfg.token_id_no)
            )
            orders_no = resp2 if isinstance(resp2, list) else resp2.get("orders", []) if isinstance(resp2, dict) else []
            return orders_yes + orders_no
        except Exception as exc:
            log.warning(f"Erro ao listar ordens: {exc}")
            return []

    def sync_inventory_from_api(self):
        """Sincroniza inventário consultando posições via balance_allowance."""
        if self.cfg.dry_run:
            return
        try:
            for token_id, label in [
                (self.cfg.token_id_yes, "YES"),
                (self.cfg.token_id_no, "NO"),
            ]:
                resp = self.client.get_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id,
                        signature_type=self.cfg.signature_type,
                    )
                )
                if isinstance(resp, dict):
                    balance = float(resp.get("balance", "0"))
                    if label == "YES":
                        self.inventory.yes_shares = balance
                    else:
                        self.inventory.no_shares = balance
            log.info(f"  Inventário sincronizado: YES={self.inventory.yes_shares}, NO={self.inventory.no_shares}")
        except Exception as exc:
            log.warning(f"  Erro ao sincronizar inventário: {exc}")

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------
    def _check_kill_switch(self):
        if self._consecutive_errors >= self._max_consecutive_errors:
            log.critical(f"Kill switch ativado: {self._consecutive_errors} erros consecutivos")
            self.cancel_all()
            sys.exit(2)

    # ------------------------------------------------------------------
    # Phase strategies
    # ------------------------------------------------------------------
    def _skew_for_inventory(self, token_label: str, spread: float) -> tuple[float, float]:
        """
        Retorna (bid_offset, ask_offset) ajustados por inventário.
        Se long no token, favorece venda (ask mais barato, bid mais longe).
        """
        half = spread / 2.0
        pos = self.inventory.yes_shares if token_label == "YES" else self.inventory.no_shares
        max_pos = self.cfg.max_position_shares

        if max_pos <= 0:
            return half, half

        skew_ratio = pos / max_pos  # -1..+1 (positivo = long)
        skew_ratio = max(-1.0, min(1.0, skew_ratio))

        # Skew: se long, bid offset maior (compra mais longe), ask offset menor (venda mais fácil)
        bid_offset = half * (1.0 + skew_ratio)
        ask_offset = half * (1.0 - skew_ratio)

        # Garantir mínimos
        tick = float(self.cfg.tick_size)
        bid_offset = max(bid_offset, tick)
        ask_offset = max(ask_offset, tick)

        return bid_offset, ask_offset

    def _should_skip_side(self, token_label: str, side: str) -> bool:
        """Retorna True se posição excede máximo neste lado."""
        pos = self.inventory.yes_shares if token_label == "YES" else self.inventory.no_shares
        if side == SIDE_BUY and pos >= self.cfg.max_position_shares:
            return True
        return False

    def run_phase_a(self):
        """Phase A (0–10 min): MM_NORMAL — post-only quotes em YES e NO."""
        self.cancel_all()
        self.refresh_midpoints()

        for token_id, label, mid in [
            (self.cfg.token_id_yes, "YES", self._last_mid_yes),
            (self.cfg.token_id_no, "NO", self._last_mid_no),
        ]:
            if mid is None:
                log.warning(f"  Sem mid para {label}, pulando quotes")
                continue

            bid_off, ask_off = self._skew_for_inventory(label, self.cfg.spread_normal)
            bid_price = mid - bid_off
            ask_price = mid + ask_off
            size = self.cfg.order_size

            if not self._should_skip_side(label, SIDE_BUY):
                self.place_quote(token_id, SIDE_BUY, bid_price, size, post_only=True)
            else:
                log.info(f"  Inventário cheio para BUY {label}, pulando bid")

            if not self._should_skip_side(label, SIDE_SELL):
                self.place_quote(token_id, SIDE_SELL, ask_price, size, post_only=True)

    def run_phase_b(self):
        """Phase B (10–12 min): NO_NEW_ENTRIES — sem novas posições, reduz inventário."""
        self.cancel_all()
        self.refresh_midpoints()
        self.sync_inventory_from_api()

        # Apenas colocar ordens que REDUZEM inventário
        for token_id, label, mid in [
            (self.cfg.token_id_yes, "YES", self._last_mid_yes),
            (self.cfg.token_id_no, "NO", self._last_mid_no),
        ]:
            if mid is None:
                continue
            pos = self.inventory.yes_shares if label == "YES" else self.inventory.no_shares

            bid_off, ask_off = self._skew_for_inventory(label, self.cfg.spread_normal)

            # Se long, apenas SELL para reduzir
            if pos > 0.5:
                ask_price = mid + ask_off
                sell_size = min(pos, self.cfg.order_size)
                self.place_quote(token_id, SIDE_SELL, ask_price, sell_size, post_only=True)

    def run_phase_c(self):
        """Phase C (12–13 min): TIGHTEN_REDUCE — spread apertado, size mínimo."""
        self.cancel_all()
        self.refresh_midpoints()
        self.sync_inventory_from_api()

        for token_id, label, mid in [
            (self.cfg.token_id_yes, "YES", self._last_mid_yes),
            (self.cfg.token_id_no, "NO", self._last_mid_no),
        ]:
            if mid is None:
                continue
            pos = self.inventory.yes_shares if label == "YES" else self.inventory.no_shares

            bid_off, ask_off = self._skew_for_inventory(label, self.cfg.spread_tight)
            size = self.cfg.min_order_size  # tamanho mínimo na fase tight

            # Se long, SELL mais agressivo
            if pos > 0.5:
                ask_price = mid + ask_off
                sell_size = min(pos, size)
                if sell_size >= self.cfg.min_order_size:
                    self.place_quote(token_id, SIDE_SELL, ask_price, sell_size, post_only=True)
            # Se flat ou levemente short, pode cotar ambos lados com size mínimo
            elif abs(pos) < 0.5:
                self.place_quote(token_id, SIDE_BUY, mid - bid_off, size, post_only=True)
                self.place_quote(token_id, SIDE_SELL, mid + ask_off, size, post_only=True)

    def run_phase_d(self):
        """Phase D (13–14 min): FLATTEN — zeragem garantida (postOnly=false se necessário)."""
        self.cancel_all()
        self.sync_inventory_from_api()

        log.info(f"  FLATTEN: inventário YES={self.inventory.yes_shares}, NO={self.inventory.no_shares}")

        if self.inventory.is_flat:
            log.info("  Já flat, nada a fazer.")
            return

        for token_id, label in [
            (self.cfg.token_id_yes, "YES"),
            (self.cfg.token_id_no, "NO"),
        ]:
            pos = self.inventory.yes_shares if label == "YES" else self.inventory.no_shares
            if abs(pos) < 0.5:
                continue

            mid = self.get_midpoint(token_id)
            if mid is None:
                # Sem mid, usar book summary para preço de emergência
                summary = self.get_book_summary(token_id)
                if pos > 0 and summary["best_bid"] is not None:
                    mid = summary["best_bid"]
                elif pos < 0 and summary["best_ask"] is not None:
                    mid = summary["best_ask"]
                else:
                    log.warning(f"  Sem preço para flatten {label}, impossível zerar")
                    continue

            if pos > 0:
                # Long → SELL agressivo (abaixo do mid)
                flatten_price = mid - self.cfg.spread_flatten
                sell_size = round(pos, 2)
                if sell_size >= self.cfg.min_order_size:
                    self.place_quote(token_id, SIDE_SELL, flatten_price, sell_size,
                                     post_only=False)  # IMPORTANTE: postOnly=false
            # Obs: posição "short" não existe diretamente no Polymarket
            # (você vende tokens, reduz balance). Mas caso a lógica local
            # indique negativo, é um bug de tracking — só garantir pos >= 0.

    def run_phase_e(self):
        """Phase E (14–15 min): DO_NOT_TRADE — cancelar tudo, validar flat."""
        self.cancel_all()
        self.sync_inventory_from_api()

        # Verificar se ficou alguma ordem
        open_orders = self.get_open_orders()
        if open_orders:
            log.warning(f"  ALERTA: {len(open_orders)} ordens ainda abertas após cancel_all!")
            # Tentar cancelar individualmente
            for o in open_orders:
                oid = o.get("id") or o.get("order_id") or o.get("orderID")
                if oid:
                    self.cancel_order(oid)

        if not self.inventory.is_flat:
            log.warning(f"  ALERTA: Posição não zerada! YES={self.inventory.yes_shares}, NO={self.inventory.no_shares}")
        else:
            log.info("  Posição zerada com sucesso.")

        log.info("  Fase E: round encerrado.")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        cfg = self.cfg
        log.info("=" * 60)
        log.info("Market Maker 15m — Bot iniciado")
        log.info(f"  DRY_RUN     = {cfg.dry_run}")
        log.info(f"  TOKEN_YES   = {cfg.token_id_yes[:20]}...")
        log.info(f"  TOKEN_NO    = {cfg.token_id_no[:20]}...")
        log.info(f"  MARKET_END  = {cfg.market_end_ts} (restam {self.time_remaining_s():.0f}s)")
        log.info(f"  ORDER_SIZE  = {cfg.order_size}")
        log.info(f"  SPREAD_NORM = {cfg.spread_normal}")
        log.info(f"  TICK_SIZE   = {cfg.tick_size}")
        log.info(f"  MIN_ORDER   = {cfg.min_order_size}")
        log.info("=" * 60)

        # Aguardar início se ainda não começou
        now = time.time()
        if now < cfg.market_start_ts:
            wait_s = cfg.market_start_ts - now
            log.info(f"Aguardando início do round ({wait_s:.0f}s)...")
            time.sleep(wait_s)

        # Verificar se já passou
        if time.time() > cfg.market_end_ts:
            log.error("MARKET_END_TS já passou. Nada a fazer.")
            return

        # Sincronizar inventário inicial
        self.sync_inventory_from_api()

        # Atualizar min_order_size real do CLOB
        summary = self.get_book_summary(cfg.token_id_yes)
        live_min = summary.get("min_order_size", cfg.min_order_size)
        if live_min and float(live_min) > cfg.min_order_size:
            log.info(f"  min_order_size atualizado do CLOB: {live_min} (era {cfg.min_order_size})")
            cfg.min_order_size = float(live_min)

        last_phase = None
        interval_s = cfg.loop_interval_ms / 1000.0

        while True:
            phase = self.current_phase()
            elapsed = self.minutes_elapsed()
            remaining = self.time_remaining_s()

            if phase != last_phase:
                log.info(f"\n{'='*40}")
                log.info(f"FASE: {phase.value} | Elapsed: {elapsed:.1f}m | Restam: {remaining:.0f}s")
                log.info(f"{'='*40}")
                last_phase = phase

            log.info(f"[{phase.value}] elapsed={elapsed:.1f}m remain={remaining:.0f}s "
                     f"inv_yes={self.inventory.yes_shares} inv_no={self.inventory.no_shares}")

            if phase == Phase.MM_NORMAL:
                self.run_phase_a()
            elif phase == Phase.NO_NEW_ENTRIES:
                self.run_phase_b()
            elif phase == Phase.TIGHTEN_REDUCE:
                self.run_phase_c()
            elif phase == Phase.FLATTEN:
                self.run_phase_d()
            elif phase == Phase.DO_NOT_TRADE:
                self.run_phase_e()
                break  # Encerrar loop

            time.sleep(interval_s)

        log.info("Bot encerrado.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket MM 15m Bot")
    parser.add_argument("--dry-run", action="store_true", help="Override DRY_RUN=true")
    parser.add_argument("--env-file", default=".env", help="Caminho do arquivo .env")
    args = parser.parse_args()

    # Carregar .env específico se informado
    if args.env_file != ".env":
        load_dotenv(args.env_file)

    cfg = BotConfig.from_env()
    if args.dry_run:
        cfg.dry_run = True
    cfg.validate()

    bot = MarketMaker(cfg)
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("\nInterrompido pelo usuário. Cancelando ordens...")
        bot.cancel_all()
        log.info("Ordens canceladas. Saindo.")


if __name__ == "__main__":
    main()
