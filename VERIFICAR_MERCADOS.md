# 🔍 Verificar se os Mercados Existem na Polymarket

## ⚠️ Problema

O bot pode estar encontrando mercados que não existem mais ou estão fechados na Polymarket.

## ✅ Solução Implementada

O bot agora:
1. **Valida** se o mercado existe e está ativo
2. **Verifica** se está aceitando ordens (`accepting_orders`)
3. **Mostra** o `minimum_order_size` do mercado
4. **Pula** mercados que não estão aceitando ordens

## 🔍 Como Verificar Manualmente

### 1. Verificar o timestamp atual:
```bash
date +%s
```

### 2. Converter para data legível:
```bash
date -d @$(date +%s)
```

### 3. Verificar o slug do mercado:
O bot usa o padrão: `eth-updown-15m-{timestamp}` e `btc-updown-15m-{timestamp}`

Exemplo: `eth-updown-15m-1769478300`

### 4. Verificar na Polymarket:
1. Acesse: https://polymarket.com
2. Procure por: "ETH Up or Down" ou "BTC Up or Down"
3. Verifique se o mercado do período atual existe
4. Verifique se está aceitando ordens

## 📊 Logs do Bot

Agora o bot mostra:
- ✅ Se o mercado foi encontrado
- ✅ Se está ativo e aceitando ordens
- ✅ O `minimum_order_size` do mercado
- ⚠️ Avisos se o mercado não está aceitando ordens

## 🔧 Se os Mercados Não Existem

Se os mercados não existem na Polymarket:

1. **Verifique o formato do slug**: O padrão pode ter mudado
2. **Verifique o timestamp**: Pode estar usando timezone incorreto
3. **Use condition IDs manualmente**: Configure no `config.json`:
   ```json
   {
     "trading": {
       "eth_condition_id": "0x...",
       "btc_condition_id": "0x..."
     }
   }
   ```

## 📝 Exemplo de Log Esperado

```
✅ Found ETH market by slug: eth-updown-15m-1769478300 | Condition ID: 0x...
✅ Market eth-updown-15m-1769478300 is accepting orders. Minimum order size: 0.01
```

Se aparecer:
```
⚠️  Market eth-updown-15m-1769478300 exists but is NOT accepting orders. Skipping...
```

Significa que o mercado existe mas está fechado ou não aceita ordens.

