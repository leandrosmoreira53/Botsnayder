# PLANO DE CORREÇÃO - Submissão de Ordens Limit

## Análise do Problema

O projeto atual (`AMM-Polymarket-Backtest`) é **apenas um backtester** - não possui código para submissão real de ordens. Para trading real com autenticação **L1 (private key + funder)**, precisamos adicionar novos componentes.

---

## Arquitetura Proposta

```
python_trading/
├── src/
│   └── trading/
│       ├── __init__.py
│       ├── polymarket_client.py    # Cliente CLOB com auth L1
│       ├── order_manager.py        # Gerenciamento de ordens limit
│       └── allowance_manager.py    # Gerenciamento de aprovações de tokens
├── config/
│   └── trading_config.py           # Configurações de trading (pk, funder)
└── requirements.txt
```

---

## Problemas Identificados e Soluções

| # | Problema | Causa | Solução |
|---|----------|-------|---------|
| 1 | **Sem cliente CLOB** | Projeto é só backtest | Criar `PolymarketClient` usando `py-clob-client` |
| 2 | **Autenticação L1** | Precisa pk + funder | Usar `signature_type=0` + `funder` parameter |
| 3 | **Sem API creds** | L1 puro não funciona | Usar `create_or_derive_api_creds()` para derivar L2 de L1 |
| 4 | **Token allowances** | USDC/CTF não aprovados | Criar script para aprovar tokens antes de trading |
| 5 | **Ordens limit** | Não implementado | Usar `OrderArgs` + `post_order(signed, OrderType.GTC)` |

---

## Fluxo de Autenticação L1 (Correto)

```python
from py_clob_client.client import ClobClient

# 1. Configuração inicial
client = ClobClient(
    host="https://clob.polymarket.com",
    key=PRIVATE_KEY,           # Sua chave privada
    chain_id=137,              # Polygon
    signature_type=0,          # EOA (MetaMask, hardware wallet)
    funder=FUNDER_ADDRESS      # Endereço com fundos
)

# 2. CRÍTICO: Derivar API creds de L1 (mesmo sem L2 separado)
client.set_api_creds(client.create_or_derive_api_creds())

# 3. Agora pode submeter ordens
```

---

## Implementação de Ordem Limit

```python
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

def submit_limit_order(client, token_id, side, price, size):
    """Submete ordem limit para Polymarket."""

    order_args = OrderArgs(
        token_id=token_id,
        price=price,          # Ex: 0.45 (45 cents)
        size=size,            # Ex: 100 shares
        side=BUY if side == "BUY" else SELL
    )

    # Criar e assinar ordem
    signed_order = client.create_order(order_args)

    # Submeter (GTC = Good Till Canceled)
    response = client.post_order(signed_order, OrderType.GTC)

    return response
```

---

## Pré-requisitos (Token Allowances)

**Antes de trading, precisa aprovar tokens:**

```python
# Contratos que precisam aprovação
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Exchange contracts para aprovar
EXCHANGE_CONTRACTS = [
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8a8EF69",  # Exchange
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",  # NegRisk Exchange
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"   # NegRisk Adapter
]
```

---

## Tarefas de Implementação

| Prioridade | Tarefa | Descrição | Status |
|------------|--------|-----------|--------|
| P0 | Criar `trading_config.py` | Configurações seguras para pk/funder | ✅ FEITO |
| P0 | Criar `polymarket_client.py` | Cliente com auth L1 correta | ✅ FEITO |
| P0 | Criar `allowance_manager.py` | Script para aprovar tokens | ✅ FEITO |
| P1 | Criar `order_manager.py` | Lógica de ordens limit | ✅ FEITO |
| P1 | Integrar com backtest | Modo live trading | ✅ FEITO |
| P2 | Adicionar CLI commands | `trade`, `approve`, `balance` | ✅ FEITO |

---

## Erros Comuns e Soluções

| Erro | Causa | Solução |
|------|-------|---------|
| `401 Unauthorized` | API creds não configuradas | Chamar `set_api_creds(create_or_derive_api_creds())` |
| `403 Forbidden` | Token allowance não setado | Executar script de aprovação |
| `Invalid signature` | `signature_type` errado | Usar `signature_type=0` para EOA |
| `Insufficient balance` | Funder sem fundos | Verificar saldo USDC no funder |

---

## Comandos CLI Implementados

```bash
# Configurar trading
python main.py setup

# Verificar saldo e allowances
python main.py balance

# Aprovar tokens (necessário antes de trading)
python main.py approve

# Submeter ordem limit simples
python main.py trade --token <TOKEN_ID> --side BUY --price 0.45 --size 100

# Submeter spread order (YES + NO)
python main.py trade --token-yes <YES_TOKEN> --token-no <NO_TOKEN> --size 100

# Listar ordens abertas
python main.py orders

# Cancelar ordens
python main.py cancel --order-id <ID>
python main.py cancel --all
```

---

## Dependências Necessárias

```
py-clob-client>=0.29.0
web3>=6.0.0
eth-account>=0.10.0
python-dotenv>=1.0.0
```

---

## Estrutura do Repositório

```
Botsnayder/
├── src/                          # Código Rust (snayderbot4 original)
│   ├── api.rs
│   ├── arbitrage.rs
│   ├── config.rs
│   ├── trader.rs
│   └── ...
├── python_trading/               # Módulo Python de trading (NOVO)
│   ├── src/trading/
│   │   ├── polymarket_client.py  # Cliente CLOB com auth L1
│   │   ├── order_manager.py      # Gerenciamento de ordens
│   │   └── allowance_manager.py  # Aprovações de tokens
│   ├── config/
│   │   └── trading_config.py     # Configurações
│   └── requirements.txt
├── Cargo.toml                    # Dependências Rust
└── PLANO_CORRECAO_LIMIT_ORDER.md # Este arquivo
```

---

## Referências

- [py-clob-client GitHub](https://github.com/Polymarket/py-clob-client)
- [Polymarket Authentication Docs](https://docs.polymarket.com/developers/CLOB/authentication)
- [Python Allowance Example](https://gist.github.com/poly-rodr/44313920481de58d5a3f6d1f8226bd5e)

---

## Status da Implementação

✅ **IMPLEMENTADO** - Código Python de trading com autenticação L1 disponível em `python_trading/`
