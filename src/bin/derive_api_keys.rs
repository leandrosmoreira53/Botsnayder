// derive_api_keys.rs
// Script para derivar API Keys L2 a partir da Private Key (L1)
//
// Uso:
//   1. Configurar variáveis de ambiente:
//      export PRIVATE_KEY="0x..."
//      export FUNDER_ADDRESS="0x..."  (opcional, se diferente da wallet)
//
//   2. Executar:
//      cargo run --bin derive_api_keys
//
//   3. Salvar as credentials no config.json

use polymarket_rs_client::ClobClient;
use std::env;

const HOST: &str = "https://clob.polymarket.com";
const POLYGON_CHAIN_ID: u64 = 137;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    println!("===========================================");
    println!("  Polymarket API Key Derivation (L1 → L2)");
    println!("===========================================\n");

    // 1. Carregar private key do ambiente
    let private_key = env::var("PRIVATE_KEY")
        .or_else(|_| env::var("POLY_PRIVATE_KEY"))
        .expect("❌ PRIVATE_KEY ou POLY_PRIVATE_KEY não configurada!\n\nUso:\n  export PRIVATE_KEY=\"0x...\"\n  cargo run --bin derive_api_keys");

    let funder = env::var("FUNDER_ADDRESS")
        .or_else(|_| env::var("POLY_FUNDER"))
        .ok();

    println!("📋 Configuração:");
    println!("   Host: {}", HOST);
    println!("   Chain ID: {} (Polygon)", POLYGON_CHAIN_ID);
    println!("   Private Key: {}...{}", &private_key[..6], &private_key[private_key.len()-4..]);
    if let Some(ref f) = funder {
        println!("   Funder: {}", f);
    }
    println!();

    // 2. Criar client L1 (usando private key para assinar)
    println!("🔐 Criando client L1 (EIP-712 signing)...");
    let mut client = ClobClient::with_l1_headers(HOST, &private_key, POLYGON_CHAIN_ID);

    // 3. Derivar API credentials (L2) a partir de L1
    println!("🔄 Derivando API credentials (L2) de L1...\n");

    let api_creds = client.create_or_derive_api_key(None).await
        .expect("❌ Falha ao derivar API credentials!\n\nPossíveis causas:\n  - Private key inválida\n  - Problemas de rede\n  - API da Polymarket offline");

    // 4. Exibir as credentials
    println!("✅ API Credentials derivadas com sucesso!\n");
    println!("===========================================");
    println!("  SUAS API CREDENTIALS (L2)");
    println!("  ⚠️  GUARDE EM LOCAL SEGURO!");
    println!("===========================================\n");

    println!("API_KEY:      {}", api_creds.api_key);
    println!("API_SECRET:   {}", api_creds.api_secret);
    println!("API_PASSPHRASE: {}", api_creds.api_passphrase);

    println!("\n===========================================");
    println!("  FORMATO PARA config.json");
    println!("===========================================\n");

    let config_json = serde_json::json!({
        "auth_method": 0,
        "api_key": api_creds.api_key,
        "api_secret": api_creds.api_secret,
        "api_passphrase": api_creds.api_passphrase,
        "private_key": private_key,
        "funder_address": funder.unwrap_or_default()
    });

    println!("{}", serde_json::to_string_pretty(&config_json)?);

    println!("\n===========================================");
    println!("  PRÓXIMOS PASSOS");
    println!("===========================================\n");
    println!("1. Copie as credentials acima para seu config.json");
    println!("2. Use auth_method: 0 (API Key) ao invés de 1 (Signature)");
    println!("3. As credentials L2 são usadas para POST /order\n");

    // 5. Testar conexão com L2
    println!("🧪 Testando conexão com L2...");
    client.set_api_creds(api_creds.clone());

    match client.get_ok().await {
        Ok(_) => println!("✅ Conexão L2 funcionando!\n"),
        Err(e) => println!("⚠️  Teste de conexão falhou: {}\n", e),
    }

    Ok(())
}
