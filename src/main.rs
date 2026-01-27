mod api;
mod arbitrage;
mod config;
mod models;
mod monitor;
mod trader;

use anyhow::{Context, Result};
use clap::Parser;
use config::{Args, Config};
use log::{info, warn};
use std::sync::Arc;

use api::PolymarketApi;
use arbitrage::ArbitrageDetector;
use monitor::MarketMonitor;
use trader::Trader;

#[tokio::main]
async fn main() -> Result<()> {
    env_logger::Builder::from_default_env()
        .filter_level(log::LevelFilter::Info)
        .init();

    let args = Args::parse();
    let config = Config::load(&args.config)?;

    info!("🚀 Starting Polymarket Arbitrage Bot");
    info!("Mode: {}", if args.simulation { "SIMULATION" } else { "PRODUCTION" });

    // Validate credentials before starting (especially important for production)
    if !args.simulation {
        info!("🔐 Validating credentials for production mode...");
        validate_credentials(&config.polymarket)?;
        info!("✅ Credentials validated successfully");
    }

    // Initialize API client
    let api = Arc::new(PolymarketApi::new(
        config.polymarket.gamma_api_url.clone(),
        config.polymarket.clob_api_url.clone(),
        config.polymarket.auth_method,
        config.polymarket.api_key.clone(),
        config.polymarket.api_secret.clone(),
        config.polymarket.api_passphrase.clone(),
        config.polymarket.private_key.clone(),
        config.polymarket.wallet_address.clone(),
    ));

    // Get market data for ETH and BTC markets
    let (eth_market_data, btc_market_data) = 
        get_or_discover_markets(&api, &config).await?;

    info!("ETH Market: {} (Condition ID: {})", eth_market_data.slug, eth_market_data.condition_id);
    info!("BTC Market: {} (Condition ID: {})", btc_market_data.slug, btc_market_data.condition_id);

    // Initialize components
    let monitor = MarketMonitor::new(
        api.clone(),
        eth_market_data,
        btc_market_data,
        config.trading.check_interval_ms,
    );
    let monitor_arc = Arc::new(monitor);

    let detector = ArbitrageDetector::new(config.trading.min_profit_threshold);
    let trader = Trader::new(
        api.clone(),
        config.trading.clone(),
        args.simulation,
    );

    // Start monitoring
    let detector_clone = detector.clone();
    let trader_arc = Arc::new(trader);
    let trader_clone = trader_arc.clone();
    let monitor_for_trading = monitor_arc.clone();
    let api_for_discovery = api.clone();
    
    // Start a background task to check pending trades periodically
    // Check every 30 seconds to catch market closures quickly (markets close after 15 minutes)
    let trader_check = trader_clone.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(tokio::time::Duration::from_secs(30)); // Check every 30 seconds
        loop {
            interval.tick().await;
            if let Err(e) = trader_check.check_pending_trades().await {
                warn!("Error checking pending trades: {}", e);
            }
        }
    });

    // Start a background task to detect new 15-minute periods and discover new markets
    let monitor_for_period_check = monitor_arc.clone();
    let api_for_period_check = api.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(tokio::time::Duration::from_secs(60)); // Check every minute
        loop {
            interval.tick().await;
            
            // Check if we need to discover new markets (new period started)
            if monitor_for_period_check.should_discover_new_markets().await {
                info!("🔄 New 15-minute period detected! Discovering new markets...");
                
                let current_time = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_secs();
                
                let mut seen_ids = std::collections::HashSet::new();
                // Get current condition IDs to avoid duplicates
                let (eth_id, btc_id) = monitor_for_period_check.get_current_condition_ids().await;
                seen_ids.insert(eth_id);
                seen_ids.insert(btc_id);
                
                // Discover new markets for current period
                match discover_market(&api_for_period_check, "ETH", "eth", current_time, &mut seen_ids).await {
                    Ok(eth_market) => {
                        seen_ids.insert(eth_market.condition_id.clone());
                        match discover_market(&api_for_period_check, "BTC", "btc", current_time, &mut seen_ids).await {
                            Ok(btc_market) => {
                                if let Err(e) = monitor_for_period_check.update_markets(eth_market, btc_market).await {
                                    warn!("Failed to update markets: {}", e);
                                }
                            }
                            Err(e) => warn!("Failed to discover new BTC market: {}", e),
                        }
                    }
                    Err(e) => warn!("Failed to discover new ETH market: {}", e),
                }
            }
        }
    });
    
    monitor_arc.start_monitoring(move |snapshot| {
        let detector = detector_clone.clone();
        let trader = trader_clone.clone();
        
        async move {
            let opportunities = detector.detect_opportunities(&snapshot);
            
            for opportunity in opportunities {
                if let Err(e) = trader.execute_arbitrage(&opportunity).await {
                    warn!("Error executing trade: {}", e);
                }
            }
        }
    }).await;

    Ok(())
}

/// Validate private_key and funder_address (wallet_address) before starting production
fn validate_credentials(config: &config::PolymarketConfig) -> Result<()> {
    use anyhow::Context;
    
    // Check if private_key is present
    let private_key = config.private_key.as_ref()
        .context("❌ private_key is missing in config.json. Required for production mode.")?;
    
    // Check if wallet_address (funder_address) is present
    let wallet_address = config.wallet_address.as_ref()
        .context("❌ funder_address is missing in config.json. Required for production mode.")?;
    
    info!("🔍 Validating private_key format...");
    
    // Validate private_key format (should be hex string, 0x prefix optional, 64 hex chars = 32 bytes)
    let key_hex = private_key.strip_prefix("0x").unwrap_or(private_key);
    if key_hex.len() != 64 {
        anyhow::bail!("❌ Invalid private_key length. Expected 64 hex characters (32 bytes), got {}. Format: 0x... or hex string", key_hex.len());
    }
    
    // Validate hex characters
    if !key_hex.chars().all(|c| c.is_ascii_hexdigit()) {
        anyhow::bail!("❌ Invalid private_key format. Must contain only hexadecimal characters (0-9, a-f, A-F)");
    }
    
    info!("✅ private_key format is valid");
    
    info!("🔍 Validating funder_address format...");
    
    // Validate wallet_address format (Ethereum address: 0x + 40 hex chars = 42 chars total)
    if !wallet_address.starts_with("0x") {
        anyhow::bail!("❌ Invalid funder_address format. Must start with '0x'. Got: {}", wallet_address);
    }
    
    if wallet_address.len() != 42 {
        anyhow::bail!("❌ Invalid funder_address length. Expected 42 characters (0x + 40 hex), got {}. Address: {}", wallet_address.len(), wallet_address);
    }
    
    let addr_hex = &wallet_address[2..];
    if !addr_hex.chars().all(|c| c.is_ascii_hexdigit()) {
        anyhow::bail!("❌ Invalid funder_address format. Must contain only hexadecimal characters after '0x'");
    }
    
    info!("✅ funder_address format is valid");
    
    info!("🔍 Verifying signer and funder relationship (signature_type={})...", config.signature_type);
    
    // Validate signer/funder relationship based on signature_type
    validate_signer_funder(private_key, wallet_address, config.signature_type)?;
    
    info!("✅ All credentials validated successfully!");
    Ok(())
}

/// Validate signer and funder relationship based on signature_type
/// 
/// - signature_type = 0 (EOA): signer == funder is required
/// - signature_type = 1 (Proxy/Magic): signer != funder is expected
/// - signature_type = 2 (Safe): signer != safe is expected
fn validate_signer_funder(
    private_key: &str,
    funder_address: &str,
    signature_type: u8, // 0=EOA, 1=Proxy/Magic, 2=Safe
) -> Result<()> {
    use ethers::prelude::*;
    use std::str::FromStr;
    
    let wallet = LocalWallet::from_str(private_key)
        .context("Failed to create wallet from private_key")?;
    let derived = format!("{:?}", wallet.address()).to_lowercase();
    let funder = funder_address.to_lowercase();

    match signature_type {
        0 => {
            // EOA: signer == funder
            if derived != funder {
                anyhow::bail!(
                    "❌ CRITICAL: private_key does NOT match funder_address (EOA mode)\n\
                     \n\
                     Derived address (signer): {}\n\
                     Funder address:           {}\n\
                     \n\
                     In EOA mode, the bot cannot sign transactions for the funder wallet.\n\
                     \n\
                     Fix options:\n\
                     1. Set funder_address = derived address (if using EOA)\n\
                     2. Use signature_type=1 if you're using Proxy/Magic\n\
                     3. Use signature_type=2 if you're using Safe",
                    derived,
                    funder
                );
            }
            info!("✅ EOA mode: signer matches funder address");
            Ok(())
        }
        1 => {
            // Proxy/Magic: signer != funder is expected
            if derived == funder {
                warn!(
                    "⚠️  signer == funder in Proxy/Magic mode (signature_type=1). \
                     Isso é incomum, mas pode acontecer. Derived={}",
                    derived
                );
            } else {
                info!(
                    "✅ Proxy/Magic mode: signer and funder are different (expected). \
                     signer={} funder={} \
                     (bot signs as signer, funds are held by funder/proxy - expected)",
                    derived,
                    funder
                );
            }
            Ok(())
        }
        2 => {
            // Safe: signer != safe is expected
            if derived == funder {
                warn!(
                    "⚠️  signer == funder in Safe mode (signature_type=2). \
                     Isso é incomum. Derived={}",
                    derived
                );
            } else {
                info!(
                    "✅ Safe mode: signer and funder are different (expected). \
                     signer={} safe={} \
                     (bot signs as signer, funds are held by safe - expected)",
                    derived,
                    funder
                );
            }
            Ok(())
        }
        _ => anyhow::bail!(
            "❌ Invalid signature_type={}. Use 0 (EOA), 1 (Proxy/Magic), or 2 (Safe).",
            signature_type
        ),
    }
}

async fn get_or_discover_markets(
    api: &PolymarketApi,
    config: &Config,
) -> Result<(crate::models::Market, crate::models::Market)> {
    use crate::models::Market;
    
    let current_time = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    
    // Try multiple discovery methods - use a set to track seen IDs
    let mut seen_ids = std::collections::HashSet::new();
    
    // Use exact slug pattern: eth-updown-15m-{timestamp} and btc-updown-15m-{timestamp}
    let eth_market = discover_market(api, "ETH", "eth", current_time, &mut seen_ids).await
        .context("Failed to discover ETH market")?;
    seen_ids.insert(eth_market.condition_id.clone());
    
    let btc_market = discover_market(api, "BTC", "btc", current_time, &mut seen_ids).await
        .context("Failed to discover BTC market")?;

    if eth_market.condition_id == btc_market.condition_id {
        anyhow::bail!("ETH and BTC markets have the same condition ID: {}. This is incorrect. Please set condition IDs manually in config.json", eth_market.condition_id);
    }

    Ok((eth_market, btc_market))
}

async fn discover_market(
    api: &PolymarketApi,
    market_name: &str,
    slug_prefix: &str,
    current_time: u64,
    seen_ids: &mut std::collections::HashSet<String>,
) -> Result<crate::models::Market> {
    use crate::models::Market;
    
    // Method 1: Try to get by slug with current timestamp (rounded to nearest 15min)
    // Pattern: btc-updown-15m-{timestamp} or eth-updown-15m-{timestamp}
    let rounded_time = (current_time / 900) * 900; // Round to nearest 15 minutes
    let slug = format!("{}-updown-15m-{}", slug_prefix, rounded_time);
    
    if let Ok(market) = api.get_market_by_slug(&slug).await {
        if !seen_ids.contains(&market.condition_id) && market.active && !market.closed {
            log::info!("✅ Found {} market by slug: {} | Condition ID: {} | Active: {} | Closed: {}", 
                      market_name, market.slug, market.condition_id, market.active, market.closed);
            
            // Verificar se o mercado realmente existe e está aceitando ordens
            if let Ok(market_details) = api.get_market(&market.condition_id).await {
                if !market_details.accepting_orders {
                    log::warn!("⚠️  Market {} exists but is NOT accepting orders. Skipping...", market.slug);
                } else {
                    log::info!("✅ Market {} is accepting orders. Minimum order size: {}", 
                              market.slug, market_details.minimum_order_size);
                    return Ok(market);
                }
            } else {
                log::warn!("⚠️  Could not verify market details for {}. Using anyway...", market.slug);
                return Ok(market);
            }
        } else {
            log::warn!("⚠️  Market {} found but not usable: active={}, closed={}, seen={}", 
                      market.slug, market.active, market.closed, seen_ids.contains(&market.condition_id));
        }
    } else {
        log::debug!("❌ Market not found by slug: {}", slug);
    }
    
    // Method 2: Try a few recent timestamps in case the current one doesn't exist yet
    for offset in 1..=3 {
        let try_time = rounded_time - (offset * 900); // Try previous 15-minute intervals
        let try_slug = format!("{}-updown-15m-{}", slug_prefix, try_time);
        log::info!("Trying previous {} market by slug: {}", market_name, try_slug);
        if let Ok(market) = api.get_market_by_slug(&try_slug).await {
            if !seen_ids.contains(&market.condition_id) && market.active && !market.closed {
                log::info!("✅ Found {} market by slug: {} | Condition ID: {} | Active: {} | Closed: {}", 
                          market_name, market.slug, market.condition_id, market.active, market.closed);
                
                // Verificar se o mercado realmente existe e está aceitando ordens
                if let Ok(market_details) = api.get_market(&market.condition_id).await {
                    if !market_details.accepting_orders {
                        log::warn!("⚠️  Market {} exists but is NOT accepting orders. Trying next...", market.slug);
                        continue;
                    } else {
                        log::info!("✅ Market {} is accepting orders. Minimum order size: {}", 
                                  market.slug, market_details.minimum_order_size);
                        return Ok(market);
                    }
                } else {
                    log::warn!("⚠️  Could not verify market details for {}. Using anyway...", market.slug);
                    return Ok(market);
                }
            }
        }
    }
    
    anyhow::bail!("Could not find active {} 15-minute up/down market. Please set condition_id in config.json", market_name)
}
