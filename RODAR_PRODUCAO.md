# 🚀 Como Rodar o Bot em Produção (LIVE)

## ⚠️ IMPORTANTE: Antes de Rodar

1. ✅ Verifique se o `config.json` está configurado corretamente:
   - `auth_method: 1` (Signature authentication)
   - `signature_type: 1` (Proxy/Magic mode)
   - `private_key` configurado
   - `funder_address` configurado
   - `max_position_size: 5.0` (seu limite de $5)

2. ✅ Certifique-se de ter saldo suficiente na carteira

## 📋 Comando para Produção

```bash
# 1. Ir para o diretório do projeto
cd /root/snayderbot/polymarket-arbitrage-bot-btc-eth-15m

# 2. Carregar ambiente Rust
source "$HOME/.cargo/env"

# 3. Rodar o bot em PRODUÇÃO (sem --simulation)
cargo run --release --bin polymarket-arbitrage-bot
```

## 🔄 Rodar em Background (Recomendado)

Para rodar o bot em background e manter rodando mesmo após fechar o terminal:

```bash
cd /root/snayderbot/polymarket-arbitrage-bot-btc-eth-15m
source "$HOME/.cargo/env"

# Rodar em background com nohup
nohup cargo run --release --bin polymarket-arbitrage-bot > bot.log 2>&1 &

# Ver o processo rodando
ps aux | grep polymarket-arbitrage-bot

# Ver os logs em tempo real
tail -f bot.log

# Parar o bot
pkill -f polymarket-arbitrage-bot
```

## 📊 Monitorar o Bot

### Ver logs em tempo real:
```bash
tail -f bot.log
```

### Ver últimas 50 linhas:
```bash
tail -n 50 bot.log
```

### Procurar por erros:
```bash
grep -i error bot.log
```

## ⚙️ Configuração Atual

- **Modo**: PRODUCTION (trades reais)
- **Max Position Size**: $5.00
- **Lotes Mínimos**: Usa `minimum_order_size` do mercado
- **Post Only**: `true` (apenas ordens maker)
- **Auth Method**: Signature (1)
- **Signature Type**: Proxy/Magic (1)

## 🛑 Parar o Bot

```bash
# Se estiver rodando em foreground: Ctrl+C

# Se estiver rodando em background:
pkill -f polymarket-arbitrage-bot
```

## ✅ O que o Bot Faz

1. **Valida credenciais** antes de iniciar
2. **Descobre mercados** ETH e BTC de 15 minutos automaticamente
3. **Monitora preços** continuamente
4. **Detecta oportunidades** de arbitragem
5. **Executa trades reais** quando encontra oportunidades lucrativas
6. **Limita cada trade** a $5.00 usando lotes mínimos
7. **Apenas 1 trade por período** (15 minutos)

## ⚠️ Avisos

- O bot executa **trades reais** com dinheiro real
- Cada trade é limitado a **$5.00 máximo**
- O bot usa **ordens maker** (post_only: true) para evitar taxas de taker
- Certifique-se de ter saldo suficiente na carteira

