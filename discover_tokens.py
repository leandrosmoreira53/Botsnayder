#!/usr/bin/env python3
"""
discover_tokens.py — Descobre TOKEN_ID_YES / TOKEN_ID_NO de um mercado Polymarket
e imprime variáveis prontas para colar no .env.

Uso:
  python discover_tokens.py --market "<slug-ou-id>" --mode slug
  python discover_tokens.py --market "<condition_id>" --mode condition
"""

import argparse
import json
import sys

import requests
from py_clob_client.client import ClobClient

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


# ---------------------------------------------------------------------------
# Gamma helpers (market discovery — sem auth)
# ---------------------------------------------------------------------------

def search_gamma_by_slug(slug: str) -> dict | None:
    """Busca mercado na Gamma API pelo slug."""
    url = f"{GAMMA_API}/markets"
    resp = requests.get(url, params={"slug": slug}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    # A Gamma retorna lista
    if isinstance(data, list) and len(data) > 0:
        return data[0]
    return None


def search_gamma_by_condition(condition_id: str) -> dict | None:
    """Busca mercado na Gamma API pelo condition_id."""
    url = f"{GAMMA_API}/markets"
    resp = requests.get(url, params={"condition_id": condition_id}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list) and len(data) > 0:
        return data[0]
    return None


def search_gamma_by_id(market_id: str) -> dict | None:
    """Busca mercado na Gamma API pelo id numérico ou string."""
    url = f"{GAMMA_API}/markets/{market_id}"
    resp = requests.get(url, timeout=15)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# CLOB helpers (book summary — sem auth)
# ---------------------------------------------------------------------------

def get_book_summary(token_id: str) -> dict:
    """Consulta order book summary do CLOB para um token_id."""
    client = ClobClient(CLOB_HOST, chain_id=CHAIN_ID)
    try:
        book = client.get_order_book(token_id)
        return {
            "asset_id": getattr(book, "asset_id", token_id),
            "min_order_size": getattr(book, "min_order_size", "?"),
            "tick_size": getattr(book, "tick_size", "?"),
            "best_bid": book.bids[0].price if book.bids else None,
            "best_ask": book.asks[0].price if book.asks else None,
            "last_trade_price": getattr(book, "last_trade_price", None),
            "has_liquidity": bool(book.bids or book.asks),
        }
    except Exception as exc:
        return {
            "asset_id": token_id,
            "min_order_size": "?",
            "tick_size": "?",
            "best_bid": None,
            "best_ask": None,
            "last_trade_price": None,
            "has_liquidity": False,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------

def extract_tokens(market: dict) -> tuple[str | None, str | None]:
    """
    Extrai token_id YES e NO de um market Gamma.
    Espera campo 'tokens' como lista de dicts com 'outcome' e 'token_id'.
    """
    tokens = market.get("tokens", [])
    # tokens pode ser string JSON em alguns endpoints
    if isinstance(tokens, str):
        tokens = json.loads(tokens)

    token_yes = None
    token_no = None
    for t in tokens:
        outcome = (t.get("outcome") or "").upper().strip()
        tid = t.get("token_id") or t.get("tokenId") or t.get("id")
        if outcome == "YES":
            token_yes = tid
        elif outcome == "NO":
            token_no = tid
    return token_yes, token_no


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Descobre TOKEN_ID_YES / TOKEN_ID_NO para um mercado Polymarket."
    )
    parser.add_argument(
        "--market", required=True,
        help="Slug, condition_id ou ID do mercado."
    )
    parser.add_argument(
        "--mode", choices=["slug", "condition", "id"], default="slug",
        help="Tipo de identificador: slug (default), condition ou id."
    )
    parser.add_argument(
        "--output-env", action="store_true",
        help="Gera arquivo .env.market com as variáveis."
    )
    args = parser.parse_args()

    # 1) Resolver mercado
    print(f"[1/4] Buscando mercado ({args.mode}): {args.market}")
    market = None
    if args.mode == "slug":
        market = search_gamma_by_slug(args.market)
    elif args.mode == "condition":
        market = search_gamma_by_condition(args.market)
    elif args.mode == "id":
        market = search_gamma_by_id(args.market)

    if market is None:
        print(f"ERRO: Mercado não encontrado para {args.mode}='{args.market}'")
        sys.exit(1)

    title = market.get("question") or market.get("title") or market.get("slug") or "?"
    condition_id = market.get("condition_id") or market.get("conditionId") or "?"
    print(f"  Mercado: {title}")
    print(f"  Condition ID: {condition_id}")

    # 2) Validar binário e extrair tokens
    print("[2/4] Extraindo tokens YES/NO...")
    token_yes, token_no = extract_tokens(market)

    if not token_yes or not token_no:
        tokens_raw = market.get("tokens", [])
        if isinstance(tokens_raw, str):
            tokens_raw = json.loads(tokens_raw)
        outcomes = [t.get("outcome", "?") for t in tokens_raw]
        print(f"ERRO: Mercado não é binário YES/NO. Outcomes encontrados: {outcomes}")
        sys.exit(1)

    print(f"  TOKEN_ID_YES = {token_yes}")
    print(f"  TOKEN_ID_NO  = {token_no}")

    # 3) Consultar book summary
    print("[3/4] Consultando order book summary (CLOB)...")
    summary_yes = get_book_summary(token_yes)
    summary_no = get_book_summary(token_no)

    for label, s in [("YES", summary_yes), ("NO", summary_no)]:
        liq = "SIM" if s["has_liquidity"] else "NÃO"
        err = f" (erro: {s['error']})" if "error" in s else ""
        print(f"  [{label}] min_order_size={s['min_order_size']}, tick_size={s['tick_size']}, "
              f"bid={s['best_bid']}, ask={s['best_ask']}, liquidez={liq}{err}")

    # 4) Output padronizado
    # Usar os valores do YES como referência (geralmente iguais)
    tick_size = summary_yes.get("tick_size") or summary_no.get("tick_size") or "0.01"
    min_order_yes = summary_yes.get("min_order_size", "?")
    min_order_no = summary_no.get("min_order_size", "?")
    # usar o maior mínimo como referência segura
    try:
        min_order_size = str(max(float(min_order_yes), float(min_order_no)))
    except (ValueError, TypeError):
        min_order_size = min_order_yes if min_order_yes != "?" else min_order_no

    print()
    print("=" * 60)
    print("VARIÁVEIS PARA .env (copie e cole):")
    print("=" * 60)
    env_lines = [
        f"TOKEN_ID_YES={token_yes}",
        f"TOKEN_ID_NO={token_no}",
        f"TICK_SIZE={tick_size}",
        f"MIN_ORDER_SIZE={min_order_size}",
    ]
    for line in env_lines:
        print(line)
    print("=" * 60)

    # Opcional: gerar .env.market
    if args.output_env:
        with open(".env.market", "w") as f:
            f.write("# Gerado por discover_tokens.py\n")
            f.write(f"# Mercado: {title}\n")
            f.write(f"# Condition ID: {condition_id}\n\n")
            for line in env_lines:
                f.write(line + "\n")
        print("\nArquivo .env.market gerado com sucesso.")

    print("\nDone.")


if __name__ == "__main__":
    main()
