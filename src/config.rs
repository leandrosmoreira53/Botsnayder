use clap::Parser;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

#[derive(Parser, Debug)]
#[command(author, version, about, long_about = None)]
pub struct Args {
    /// Run in simulation mode (no real trades)
    #[arg(short, long, default_value_t = false)]
    pub simulation: bool,

    /// Configuration file path
    #[arg(short, long, default_value = "config.json")]
    pub config: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub polymarket: PolymarketConfig,
    pub trading: TradingConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PolymarketConfig {
    pub gamma_api_url: String,
    pub clob_api_url: String,
    pub ws_url: String,
    #[serde(default)]
    pub auth_method: u8, // 0 = API Key Credentials, 1 = Assinatura (Signature)
    #[serde(default)]
    pub signature_type: u8, // 0 = EOA (signer == funder), 1 = Proxy/Magic (signer != funder), 2 = Safe (signer != safe)
    pub api_key: Option<String>, // Para auth_method = 0: CLOB_API_KEY
    #[serde(skip_serializing_if = "Option::is_none")]
    pub api_secret: Option<String>, // Para auth_method = 0: CLOB_API_SECRET
    #[serde(skip_serializing_if = "Option::is_none")]
    pub api_passphrase: Option<String>, // Para auth_method = 0: CLOB_PASS_PHRASE
    #[serde(skip_serializing_if = "Option::is_none")]
    pub private_key: Option<String>, // Chave privada para autenticação por assinatura (formato: 0x...)
    #[serde(skip_serializing_if = "Option::is_none", rename = "funder_address")]
    pub wallet_address: Option<String>, // Endereço da carteira (formato: 0x...) - lê "funder_address" do JSON
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TradingConfig {
    pub min_profit_threshold: f64,
    pub max_position_size: f64, // Saldo total disponível
    #[serde(default = "default_max_concurrent_trades")]
    pub max_concurrent_trades: u32, // Quantos trades simultâneos (divide o saldo)
    pub eth_condition_id: Option<String>,
    pub btc_condition_id: Option<String>,
    pub check_interval_ms: u64,
    #[serde(default)]
    pub post_only: bool, // Se true, garante que ordens sejam apenas LIMIT (maker), não serão executadas imediatamente
    #[serde(default)]
    pub use_market_orders: bool, // Se true, usa MARKET orders (execução garantida). Se false, usa LIMIT com preço melhorado
    #[serde(default = "default_price_improvement")]
    pub price_improvement_pct: f64, // Percentual para melhorar preço LIMIT (ex: 0.1 = +0.1% para garantir execução como taker)
}

fn default_price_improvement() -> f64 {
    0.1 // 0.1% por padrão
}

fn default_max_concurrent_trades() -> u32 {
    1 // Por padrão, apenas 1 trade por vez
}

impl Default for Config {
    fn default() -> Self {
        Self {
            polymarket: PolymarketConfig {
                gamma_api_url: "https://gamma-api.polymarket.com".to_string(),
                clob_api_url: "https://clob.polymarket.com".to_string(),
                ws_url: "wss://clob-ws.polymarket.com".to_string(),
                auth_method: 0, // Padrão: API Key Credentials (0), use 1 para assinatura
                signature_type: 0, // Padrão: EOA (0), use 1 para Proxy/Magic, 2 para Safe
                api_key: None,
                api_secret: None,
                api_passphrase: None,
                private_key: None,
                wallet_address: None,
            },
            trading: TradingConfig {
                min_profit_threshold: 0.01,
                max_position_size: 100.0,
                max_concurrent_trades: 1, // Por padrão, 1 trade por vez
                eth_condition_id: None,
                btc_condition_id: None,
                check_interval_ms: 1000,
                post_only: false, // Padrão: false (pode executar imediatamente como taker)
                use_market_orders: false, // Padrão: LIMIT orders com preço melhorado
                price_improvement_pct: 0.1, // Padrão: +0.1% para garantir execução
            },
        }
    }
}

impl Config {
    pub fn load(path: &PathBuf) -> anyhow::Result<Self> {
        if path.exists() {
            let content = std::fs::read_to_string(path)?;
            Ok(serde_json::from_str(&content)?)
        } else {
            let config = Config::default();
            let content = serde_json::to_string_pretty(&config)?;
            std::fs::write(path, content)?;
            Ok(config)
        }
    }
}

