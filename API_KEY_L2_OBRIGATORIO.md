# ⚠️ API KEY L2 OBRIGATÓRIA PARA PRODUÇÃO

## 🚨 IMPORTANTE: Proxy/Safe em Produção REQUER API Key L2

Para operar em **produção live** com `signature_type: 1` (Proxy/Magic) ou `signature_type: 2` (Safe), você **DEVE** configurar:

### ✅ Credenciais Obrigatórias:

1. **`private_key`** - Chave privada do signer (para assinar EIP-712)
2. **`funder_address`** - Endereço do proxy/safe (quem tem os fundos)
3. **`api_key`** - API Key L2 da Polymarket (OBRIGATÓRIA)
4. **`api_secret`** - API Secret L2 da Polymarket (OBRIGATÓRIA)
5. **`api_passphrase`** - API Passphrase L2 da Polymarket (OBRIGATÓRIA)

## 🔐 Por Que API Key L2 é Obrigatória?

- **`private_key`** + **`funder_address`** = Assinatura EIP-712 (autoriza a ordem)
- **`api_key`** + **`api_secret`** + **`api_passphrase`** = Autenticação L2 (autoriza o request no CLOB)

**Sem API Key L2, você receberá erro 401 "Unauthorized/Invalid api key"**

## 📝 Como Obter API Key L2

1. Acesse sua conta na Polymarket
2. Vá em Settings → API Keys
3. Crie uma nova API Key L2
4. **IMPORTANTE**: A API Key deve estar vinculada ao mesmo `funder_address`/proxy que você está usando
5. Copie:
   - API Key
   - API Secret
   - API Passphrase

## ⚙️ Configuração no config.json

```json
{
  "polymarket": {
    "auth_method": 1,
    "signature_type": 1,
    "private_key": "0x...",
    "funder_address": "0x...",
    "api_key": "SUA_API_KEY_L2",
    "api_secret": "SEU_API_SECRET_L2",
    "api_passphrase": "SUA_API_PASSPHRASE_L2"
  }
}
```

## 🛑 Hard Stop em 401

O bot agora tem **hard stop** se receber 401 em produção:

- ✅ Para o bot imediatamente
- ✅ Mostra mensagem de erro clara
- ✅ Previne spam e rate limiting
- ✅ Protege sua conta

## ⚠️ O Que Você Pode Fazer SEM API Key

- ✅ Derivar endereço do signer
- ✅ Assinar payloads localmente
- ✅ Consultar saldo on-chain (USDC, allowance)
- ✅ Rodar simulação/backtest
- ✅ Monitorar oportunidades

## ❌ O Que Você NÃO Pode Fazer SEM API Key

- ❌ Enviar ordens no CLOB
- ❌ Cancelar ordens
- ❌ Ler saldo/posições via CLOB API
- ❌ Operar em produção live

## 🔧 Verificação

Antes de rodar em produção, verifique:

```bash
# Verificar se todas as credenciais estão configuradas
cat config.json | grep -E "(api_key|api_secret|api_passphrase|private_key|funder_address)"
```

Todos os campos devem estar preenchidos (não `null`) para produção.

