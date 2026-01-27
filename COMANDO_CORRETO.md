# ✅ Comando Correto para Executar o Bot

## ⚠️ Importante: Diretório Correto

Você precisa estar no diretório do projeto antes de executar:

```bash
# 1. Ir para o diretório do projeto
cd /root/snayderbot/polymarket-arbitrage-bot-btc-eth-15m

# 2. Carregar ambiente Rust
source "$HOME/.cargo/env"

# 3. Executar o bot
```

## 🚀 Comandos

### Modo PRODUCTION (padrão - com validação de credenciais)
```bash
cd /root/snayderbot/polymarket-arbitrage-bot-btc-eth-15m
source "$HOME/.cargo/env"
cargo run --release --bin polymarket-arbitrage-bot
```

### Modo SIMULATION (sem validação, sem trades reais)
```bash
cd /root/snayderbot/polymarket-arbitrage-bot-btc-eth-15m
source "$HOME/.cargo/env"
cargo run --release --bin polymarket-arbitrage-bot -- --simulation
```

## 🔐 Validação em Produção

Quando você executa em modo produção (padrão, sem `--simulation`), o bot valida automaticamente:

1. ✅ `private_key` está presente e tem formato válido
2. ✅ `funder_address` está presente e tem formato válido  
3. ✅ `private_key` corresponde ao `funder_address` (deriva o endereço e compara)

Se qualquer validação falhar, o bot **NÃO inicia** e mostra uma mensagem de erro clara.

## 📝 Exemplo de Saída (Produção)

```
🚀 Starting Polymarket Arbitrage Bot
Mode: PRODUCTION
🔐 Validating credentials for production mode...
🔍 Validating private_key format...
✅ private_key format is valid
🔍 Validating funder_address format...
✅ funder_address format is valid
🔍 Verifying private_key matches funder_address...
✅ private_key matches funder_address
✅ All credentials validated successfully!
```

## 💡 Dica: Criar Alias

Adicione ao seu `~/.bashrc`:

```bash
alias polymarket-bot='cd /root/snayderbot/polymarket-arbitrage-bot-btc-eth-15m && source "$HOME/.cargo/env" && cargo run --release --bin polymarket-arbitrage-bot'
alias polymarket-bot-prod='cd /root/snayderbot/polymarket-arbitrage-bot-btc-eth-15m && source "$HOME/.cargo/env" && cargo run --release --bin polymarket-arbitrage-bot'
```

Depois execute:
```bash
source ~/.bashrc
```

Agora você pode usar:
```bash
polymarket-bot          # Modo production (padrão, com validação)
polymarket-bot-sim      # Modo simulation (sem trades reais)
```

