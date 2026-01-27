# 🔑 Explicação sobre API Key vs Endereço de Carteira

## ⚠️ Diferença Importante

**O que você tem:** Um **endereço de carteira Ethereum** (`0x7f787eed9edbd984e1d6f57693e40a15349a91f`)

**O que o bot precisa:** Uma **API Key** (token de autenticação)

São coisas diferentes!

## 📋 O que é cada um:

### Endereço de Carteira (Wallet Address)
- É o endereço público da sua carteira no Polymarket
- Formato: `0x` seguido de 40 caracteres hexadecimais
- Usado para identificar sua conta
- **NÃO é usado para autenticação na API**

### API Key
- É um token de autenticação gerado pelo Polymarket
- Formato: geralmente uma string longa alfanumérica
- Usado para autenticar requisições à API
- **É isso que você precisa para fazer trades**

## 🔍 Como Obter a API Key Correta

O Polymarket pode usar diferentes métodos de autenticação:

### Opção 1: API Key Tradicional
1. Acesse sua conta no Polymarket
2. Vá em **Settings** → **API** ou **Developer Settings**
3. Procure por "API Key" ou "Generate API Key"
4. Gere uma nova chave
5. Copie a chave (geralmente uma string longa)

### Opção 2: Autenticação por Assinatura (mais comum em DeFi)
O Polymarket CLOB API pode usar autenticação baseada em assinatura de mensagens com sua chave privada. Nesse caso:
- Você precisa da **chave privada** da carteira (NÃO compartilhe!)
- O bot precisaria ser modificado para assinar mensagens

## 🧪 Teste Primeiro

**IMPORTANTE**: Teste primeiro em modo de simulação para ver se funciona:

```bash
cd /root/snayderbot/polymarket-arbitrage-bot-btc-eth-15m
source "$HOME/.cargo/env"
cargo run -- --simulation
```

O modo de simulação:
- ✅ Funciona SEM API key (só lê dados)
- ✅ Detecta oportunidades de arbitragem
- ✅ Mostra o que faria, mas não executa trades reais
- ✅ Permite testar se tudo está funcionando

## 🚀 Para Produção

Para executar trades reais, você precisa:

1. **API Key válida** OU
2. **Modificar o bot** para usar autenticação por assinatura (se o Polymarket usar isso)

## 📞 Próximos Passos

1. **Teste em simulação primeiro** - veja se detecta oportunidades
2. **Verifique a documentação do Polymarket** sobre autenticação da API
3. **Se não encontrar API key tradicional**, pode ser que precise modificar o código para usar assinatura de mensagens

## ⚠️ Aviso de Segurança

**NUNCA compartilhe sua chave privada!** Se o Polymarket usar autenticação por assinatura, a chave privada deve ficar segura e nunca ser exposta.

