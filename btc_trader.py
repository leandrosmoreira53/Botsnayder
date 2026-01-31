#!/usr/bin/env python3
"""
Script automatico para fazer trades em mercados "Bitcoin Up or Down 15m"
Busca novos mercados a cada 15 minutos e executa trades automaticamente.

Usa a mesma logica de descoberta de mercados do bot Rust:
- Calcula o slug com timestamp arredondado para 15 minutos
- Busca via Gamma API: https://gamma-api.polymarket.com/events/slug/{slug}
- Fallback para periodos anteriores se o mercado atual nao existir
"""
from dotenv import load_dotenv
import os
import time
import requests
from datetime import datetime
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, SELL

GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"


def find_latest_btc_market(slug_prefix="btc"):
    """
    Busca o mercado Bitcoin Up/Down 15m mais recente usando a Gamma API.
    Usa a mesma logica do bot Rust: slug = {prefix}-updown-15m-{rounded_timestamp}
    """
    current_time = int(time.time())
    rounded_time = (current_time // 900) * 900  # Arredonda para 15 minutos

    # Tenta o periodo atual e ate 3 periodos anteriores
    for offset in range(4):
        try_time = rounded_time - (offset * 900)
        slug = f"{slug_prefix}-updown-15m-{try_time}"

        label = "atual" if offset == 0 else f"anterior (-{offset * 15}min)"
        print(f"   Tentando slug {label}: {slug}")

        try:
            url = f"{GAMMA_API_URL}/events/slug/{slug}"
            resp = requests.get(url, timeout=10)

            if resp.status_code != 200:
                print(f"   -> Status {resp.status_code}, tentando proximo...")
                continue

            event = resp.json()

            # A resposta e um objeto event com array "markets"
            markets = event.get("markets", [])
            if not markets:
                print(f"   -> Evento encontrado mas sem mercados")
                continue

            market = markets[0]
            condition_id = market.get("conditionId") or market.get("condition_id")
            question = market.get("question", slug)
            active = market.get("active", False)
            closed = market.get("closed", True)

            if not active or closed:
                print(f"   -> Mercado encontrado mas inativo/fechado (active={active}, closed={closed})")
                continue

            print(f"   -> Encontrado: {question}")
            print(f"      Condition ID: {condition_id}")
            print(f"      Active: {active}, Closed: {closed}")

            # Verifica se esta aceitando ordens via CLOB API
            try:
                clob_resp = requests.get(f"{CLOB_API_URL}/markets/{condition_id}", timeout=10)
                if clob_resp.status_code == 200:
                    clob_data = clob_resp.json()
                    accepting = clob_data.get("accepting_orders", False)
                    min_size = clob_data.get("minimum_order_size", "N/A")
                    if not accepting:
                        print(f"   -> Mercado NAO esta aceitando ordens. Tentando proximo...")
                        continue
                    print(f"      Aceitando ordens: sim | Min order size: {min_size}")
            except Exception as e:
                print(f"   -> Aviso: nao foi possivel verificar CLOB: {e}")

            return market, condition_id

        except Exception as e:
            print(f"   -> Erro ao buscar slug {slug}: {e}")
            continue

    print("Nenhum mercado BTC Up/Down 15m ativo encontrado nos ultimos 4 periodos")
    return None, None


def get_market_tokens(condition_id, gamma_market=None):
    """
    Obtem os tokens (Up e Down) de um mercado.
    Tenta primeiro via CLOB API, fallback para tokens da Gamma API.

    IMPORTANTE: Esses mercados usam outcomes "Up"/"Down", NAO "Yes"/"No".
    """
    # Fonte 1: CLOB API
    try:
        resp = requests.get(f"{CLOB_API_URL}/markets/{condition_id}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            tokens = data.get("tokens", [])
            if tokens:
                print(f"   Tokens da CLOB API ({len(tokens)}):")
                for t in tokens:
                    tid = t.get("token_id") or t.get("tokenId")
                    outcome = t.get("outcome", "?")
                    print(f"      outcome={outcome} token_id={tid}")
                up_token, down_token = _extract_tokens(tokens)
                if up_token and down_token:
                    return up_token, down_token
    except Exception as e:
        print(f"   Aviso: CLOB API falhou: {e}")

    # Fonte 2: Gamma API (tokens ja vieram no market object)
    if gamma_market:
        tokens = gamma_market.get("tokens", [])
        if tokens:
            print(f"   Tokens da Gamma API ({len(tokens)}):")
            for t in tokens:
                tid = t.get("token_id") or t.get("tokenId")
                outcome = t.get("outcome", "?")
                print(f"      outcome={outcome} token_id={tid}")
            up_token, down_token = _extract_tokens(tokens)
            if up_token and down_token:
                return up_token, down_token

    # Fonte 3: clobTokenIds + outcomes do market da Gamma API
    if gamma_market:
        try:
            import json
            clob_ids_raw = gamma_market.get("clobTokenIds", "[]")
            outcomes_raw = gamma_market.get("outcomes", "[]")
            clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw

            if clob_ids and outcomes and len(clob_ids) == len(outcomes):
                print(f"   Tokens de clobTokenIds/outcomes:")
                tokens = []
                for tid, outcome in zip(clob_ids, outcomes):
                    print(f"      outcome={outcome} token_id={tid}")
                    tokens.append({"token_id": tid, "outcome": outcome})
                up_token, down_token = _extract_tokens(tokens)
                if up_token and down_token:
                    return up_token, down_token
        except Exception as e:
            print(f"   Aviso: fallback clobTokenIds falhou: {e}")

    print("   Nenhum token Up/Down encontrado em nenhuma fonte")
    return None, None


def _extract_tokens(tokens):
    """
    Extrai tokens Up e Down de uma lista de tokens.
    Suporta outcomes: Up/Down, Yes/No (case-insensitive).
    Se houver exatamente 2 tokens sem outcome reconhecido, usa posicao [0]=Up, [1]=Down.
    """
    up_token = None
    down_token = None

    for token in tokens:
        outcome = token.get("outcome", "").strip().upper()
        if outcome in ("UP", "YES"):
            up_token = token
        elif outcome in ("DOWN", "NO"):
            down_token = token

    # Fallback: se tem exatamente 2 tokens e nao achou por nome, usa posicao
    if not up_token and not down_token and len(tokens) == 2:
        print("   (usando posicao: [0]=Up, [1]=Down)")
        up_token = tokens[0]
        down_token = tokens[1]

    return up_token, down_token
        import traceback
        traceback.print_exc()
        return None, None


def get_price(token_id, side="BUY"):
    """
    Obtem o preco de um token via CLOB API
    """
    try:
        resp = requests.get(
            f"{CLOB_API_URL}/price",
            params={"token_id": token_id, "side": side},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return float(data.get("price", 0))
    except Exception:
        pass
    return None


def place_order(client, token_id, price, size, side=BUY):
    """
    Cria e envia uma ordem limit via py_clob_client
    """
    try:
        order = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )

        signed = client.create_order(order)
        resp = client.post_order(signed, OrderType.GTC)

        return resp
    except Exception as e:
        print(f"   Erro ao criar ordem: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    load_dotenv()

    # Configuracoes
    HOST = os.getenv("CLOB_API_URL", CLOB_API_URL)
    CHAIN_ID = POLYGON
    PRIVATE_KEY = os.getenv("PK")
    FUNDER = os.getenv("FUNDER")

    # Configuracoes de trading
    INTERVAL_MINUTES = int(os.getenv("TRADE_INTERVAL_MINUTES", "15"))
    ORDER_PRICE = float(os.getenv("ORDER_PRICE", "0.50"))
    ORDER_SIZE = float(os.getenv("ORDER_SIZE", "10.0"))
    TOKEN_SIDE = os.getenv("TOKEN_SIDE", "UP").upper()  # UP ou DOWN
    TRADE_ENABLED = os.getenv("AUTO_TRADE_ENABLED", "true").lower() == "true"

    if not PRIVATE_KEY:
        print("Erro: PK nao configurada no arquivo .env")
        return

    signature_type = 1 if FUNDER else 0

    print("=" * 60)
    print("Bot Automatico - Bitcoin Up/Down 15m")
    print("=" * 60)
    print(f"Host: {HOST}")
    print(f"Chain ID: {CHAIN_ID}")
    print(f"Signature Type: {signature_type} ({'Proxy/Email' if signature_type == 1 else 'EOA/MetaMask'})")
    print(f"Intervalo: {INTERVAL_MINUTES} minutos")
    print(f"Preco: ${ORDER_PRICE:.4f}")
    print(f"Tamanho: {ORDER_SIZE} shares")
    print(f"Lado: {TOKEN_SIDE}")
    print(f"Auto Trade: {'Ativado' if TRADE_ENABLED else 'Desativado (apenas monitoramento)'}")
    print(f"Descoberta: Gamma API slug-based (btc-updown-15m-{{timestamp}})")
    print("=" * 60)

    # Cria o cliente autenticado para ordens
    client = ClobClient(
        HOST,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        signature_type=signature_type,
        funder=FUNDER if FUNDER else None,
    )

    print("\nConfigurando credenciais da API...")
    client.set_api_creds(client.create_or_derive_api_creds())
    print("Cliente configurado!\n")

    last_market_condition_id = None
    iteration = 0

    try:
        while True:
            iteration += 1
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            print(f"\n{'='*60}")
            print(f"Iteracao #{iteration} - {current_time}")
            print(f"{'='*60}")

            # Busca o mercado mais recente via Gamma API (slug-based)
            print("Buscando mercado BTC Up/Down 15m via Gamma API...")
            market, condition_id = find_latest_btc_market("btc")

            if not market or not condition_id:
                print("Nenhum mercado encontrado. Tentando novamente no proximo intervalo...")
                time.sleep(INTERVAL_MINUTES * 60)
                continue

            # Verifica se e um mercado novo
            if condition_id == last_market_condition_id:
                print("Mesmo mercado da ultima iteracao. Aguardando novo mercado...")
            else:
                print("Novo mercado detectado!")
                last_market_condition_id = condition_id

                # Obtem os tokens (Up e Down)
                print("\nObtendo tokens do mercado...")
                up_token, down_token = get_market_tokens(condition_id, gamma_market=market)

                if not up_token or not down_token:
                    print("Nao foi possivel obter tokens. Pulando este mercado...")
                    time.sleep(INTERVAL_MINUTES * 60)
                    continue

                up_id = up_token.get("token_id") or up_token.get("tokenId")
                down_id = down_token.get("token_id") or down_token.get("tokenId")

                print(f"Tokens encontrados:")
                print(f"   UP   Token ID: {up_id}")
                print(f"   DOWN Token ID: {down_id}")

                # Obtem precos atuais
                up_price = get_price(up_id, "BUY")
                down_price = get_price(down_id, "BUY")
                if up_price is not None and down_price is not None:
                    print(f"\nPrecos atuais:")
                    print(f"   UP:   ${up_price:.4f}")
                    print(f"   DOWN: ${down_price:.4f}")
                    print(f"   Total: ${up_price + down_price:.4f}")

                # Executa trade se habilitado
                if TRADE_ENABLED:
                    target_token = up_token if TOKEN_SIDE in ("UP", "YES") else down_token
                    token_id = target_token.get("token_id") or target_token.get("tokenId")

                    print(f"\nPreparando ordem {TOKEN_SIDE}...")
                    print(f"   Token ID: {token_id}")
                    print(f"   Preco: ${ORDER_PRICE:.4f}")
                    print(f"   Tamanho: {ORDER_SIZE} shares")

                    if signature_type == 0:
                        print("\n   AVISO: Certifique-se de que configurou os allowances!")

                    resp = place_order(client, token_id, ORDER_PRICE, ORDER_SIZE, BUY)

                    if resp:
                        print(f"\nTrade executado com sucesso!")
                        print(f"Resposta: {resp}")
                    else:
                        print(f"\nFalha ao executar trade")
                else:
                    print("\nAuto-trade desativado. Apenas monitorando...")

            # Aguarda o proximo intervalo
            wait_seconds = INTERVAL_MINUTES * 60
            next_time = datetime.now().timestamp() + wait_seconds
            next_time_str = datetime.fromtimestamp(next_time).strftime("%Y-%m-%d %H:%M:%S")

            print(f"\nAguardando {INTERVAL_MINUTES} minutos ate {next_time_str}...")
            print("(Pressione Ctrl+C para parar)")

            time.sleep(wait_seconds)

    except KeyboardInterrupt:
        print("\n\nBot interrompido pelo usuario")
        print("Encerrando...")
    except Exception as e:
        print(f"\nErro fatal: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
