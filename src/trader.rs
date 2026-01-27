use crate::api::PolymarketApi;
use crate::models::*;
use crate::config::TradingConfig;
use anyhow::Result;
use log::{info, warn, debug};
use rust_decimal::Decimal;
use std::sync::Arc;
use tokio::sync::Mutex;
use std::collections::HashMap;
use std::time::{Instant, Duration};

#[derive(Clone)]
struct CachedMarketData {
    market: MarketDetails,
    cached_at: Instant,
}

pub struct Trader {
    api: Arc<PolymarketApi>,
    config: TradingConfig,
    simulation_mode: bool,
    total_profit: Arc<Mutex<f64>>,
    trades_executed: Arc<Mutex<u64>>,
    pending_trades: Arc<Mutex<HashMap<String, PendingTrade>>>, // Key: eth_condition_id + btc_condition_id
    market_cache: Arc<Mutex<HashMap<String, CachedMarketData>>>, // Key: condition_id, cache for 60 seconds
}

impl Trader {
    pub fn new(api: Arc<PolymarketApi>, config: TradingConfig, simulation_mode: bool) -> Self {
        Self {
            api,
            config,
            simulation_mode,
            total_profit: Arc::new(Mutex::new(0.0)),
            trades_executed: Arc::new(Mutex::new(0)),
            pending_trades: Arc::new(Mutex::new(HashMap::new())),
            market_cache: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Check and settle pending trades when markets close
    pub async fn check_pending_trades(&self) -> Result<()> {
        let mut pending = self.pending_trades.lock().await;
        let mut to_remove = Vec::new();
        
        // Only check trades that are at least 14 minutes old (markets close after 15 minutes)
        let min_age = Duration::from_secs(14 * 60);
        
        let pending_count = pending.len();
        if pending_count > 0 {
            debug!("Checking {} pending trades for market closure...", pending_count);
        }
        
        for (key, trade) in pending.iter() {
            let age = trade.timestamp.elapsed();
            
            // Skip checking if trade is too recent (markets won't be closed yet)
            if age < min_age {
                debug!("Trade {} is too recent (age: {:.1}s, need: {:.1}s), skipping", 
                       key, age.as_secs_f64(), min_age.as_secs_f64());
                continue;
            }
            
            info!("🔍 Checking market closure for trade {} (age: {:.1} minutes)", 
                  key, age.as_secs_f64() / 60.0);
            
            // Check if markets are closed (using cached data when possible)
            let (eth_closed, eth_winner) = self.check_market_result_cached(&trade.eth_condition_id, &trade.eth_token_id).await?;
            let (btc_closed, btc_winner) = self.check_market_result_cached(&trade.btc_condition_id, &trade.btc_token_id).await?;
            
            info!("   ETH Market ({}): closed={}, winner={}", 
                  &trade.eth_condition_id[..16], eth_closed, eth_winner);
            info!("   BTC Market ({}): closed={}, winner={}", 
                  &trade.btc_condition_id[..16], btc_closed, btc_winner);
            
            if eth_closed && btc_closed {
                // Both markets closed, sell/redeem winning tokens and calculate actual profit
                if !self.simulation_mode {
                    // In production mode, try to sell winning tokens (they're worth $1 each)
                    self.sell_winning_tokens(&trade, eth_winner, btc_winner).await;
                }
                
                let actual_profit = self.calculate_actual_profit(&trade, eth_winner, btc_winner);
                
                let mut total = self.total_profit.lock().await;
                *total += actual_profit;
                let total_profit = *total;
                drop(total);
                
                info!(
                    "💰 Market Closed - ETH Winner: {}, BTC Winner: {} | Actual Profit: ${:.4} | Total Profit: ${:.2}",
                    if eth_winner { "WON" } else { "LOST" },
                    if btc_winner { "WON" } else { "LOST" },
                    actual_profit,
                    total_profit
                );
                
                to_remove.push(key.clone());
            } else {
                info!("   ⏳ Markets not both closed yet (ETH: {}, BTC: {}), will check again...", 
                      eth_closed, btc_closed);
            }
        }
        
        for key in to_remove {
            pending.remove(&key);
        }
        
        Ok(())
    }

    async fn check_market_result_cached(&self, condition_id: &str, token_id: &str) -> Result<(bool, bool)> {
        // Check cache first (cache for 60 seconds)
        let cache_ttl = Duration::from_secs(60);
        let mut cache = self.market_cache.lock().await;
        
        // Check if we have cached data that's still valid
        if let Some(cached) = cache.get(condition_id) {
            if cached.cached_at.elapsed() < cache_ttl {
                // Use cached data
                let market = &cached.market;
                if market.closed {
                    let winner = market.tokens.iter()
                        .find(|t| t.token_id == token_id)
                        .map(|t| t.winner)
                        .unwrap_or(false);
                    debug!("Using cached market data for condition_id: {}", condition_id);
                    return Ok((true, winner));
                } else {
                    debug!("Using cached market data (not closed yet) for condition_id: {}", condition_id);
                    return Ok((false, false));
                }
            }
        }
        
        // Cache miss or expired - fetch from API
        drop(cache);
        match self.api.get_market(condition_id).await {
            Ok(market) => {
                // Update cache
                let mut cache = self.market_cache.lock().await;
                cache.insert(condition_id.to_string(), CachedMarketData {
                    market: market.clone(),
                    cached_at: Instant::now(),
                });
                drop(cache);
                
                if market.closed {
                    // Find our token and check if it's the winner
                    let winner = market.tokens.iter()
                        .find(|t| t.token_id == token_id)
                        .map(|t| t.winner)
                        .unwrap_or(false);
                    Ok((true, winner))
                } else {
                    Ok((false, false))
                }
            }
            Err(e) => {
                warn!("Failed to fetch market {}: {}", condition_id, e);
                Ok((false, false))
            }
        }
    }

    /// Sell winning tokens when markets close (production mode only)
    async fn sell_winning_tokens(&self, trade: &PendingTrade, eth_winner: bool, btc_winner: bool) {
        // When markets close, winning tokens are worth $1 each
        // We should sell them to realize the profit
        let sell_price = "1.0"; // Winning tokens are worth $1 when market closes
        
        if eth_winner {
            // Sell ETH Up token (it won, worth $1)
            let sell_order = OrderRequest {
                token_id: trade.eth_token_id.clone(),
                side: "SELL".to_string(),
                size: format!("{:.6}", trade.units),
                price: sell_price.to_string(),
                order_type: "LIMIT".to_string(),
                post_only: if self.config.post_only { Some(true) } else { None },
            };
            
            match self.api.place_order(&sell_order, !self.simulation_mode).await {
                Ok(_) => {
                    info!("✅ Sold {} units of ETH Up token (winner) at $1.00", trade.units);
                }
                Err(e) => {
                    warn!("⚠️  Failed to sell ETH Up token: {}", e);
                }
            }
        }
        
        if btc_winner {
            // Sell BTC Down token (it won, worth $1)
            let sell_order = OrderRequest {
                token_id: trade.btc_token_id.clone(),
                side: "SELL".to_string(),
                size: format!("{:.6}", trade.units),
                price: sell_price.to_string(),
                order_type: "LIMIT".to_string(),
                post_only: if self.config.post_only { Some(true) } else { None },
            };
            
            match self.api.place_order(&sell_order, !self.simulation_mode).await {
                Ok(_) => {
                    info!("✅ Sold {} units of BTC Down token (winner) at $1.00", trade.units);
                }
                Err(e) => {
                    warn!("⚠️  Failed to sell BTC Down token: {}", e);
                }
            }
        }
        
        if !eth_winner && !btc_winner {
            warn!("⚠️  Both tokens lost - nothing to sell (both worth $0)");
        }
    }

    fn calculate_actual_profit(&self, trade: &PendingTrade, eth_winner: bool, btc_winner: bool) -> f64 {
        // We bought ETH Up + BTC Down
        // When markets close:
        // - If ETH Up wins: we get $1 per unit
        // - If BTC Down wins: we get $1 per unit
        // - If both win: we get $2 per unit
        // - If both lose: we get $0 per unit
        
        let payout_per_unit = if eth_winner && btc_winner {
            2.0 // Both won! (ETH went UP, BTC went DOWN)
        } else if eth_winner || btc_winner {
            1.0 // One won (break even or small profit)
        } else {
            0.0 // Both lost! (ETH went DOWN, BTC went UP) - TOTAL LOSS
        };
        
        let total_payout = payout_per_unit * trade.units;
        let actual_profit = total_payout - trade.investment_amount;
        
        if actual_profit < 0.0 {
            warn!("⚠️  LOSS: Both tokens lost! Lost ${:.4} on this trade", -actual_profit);
        }
        
        actual_profit
    }

    /// Execute arbitrage trade
    pub async fn execute_arbitrage(&self, opportunity: &ArbitrageOpportunity) -> Result<()> {
        if self.simulation_mode {
            self.simulate_trade(opportunity).await
        } else {
            self.execute_real_trade(opportunity).await
        }
    }

    async fn simulate_trade(&self, opportunity: &ArbitrageOpportunity) -> Result<()> {
        info!(
            "🔍 SIMULATION: Arbitrage opportunity detected!"
        );
        info!(
            "   ETH Up Token Price: ${:.4}",
            opportunity.eth_up_price
        );
        info!(
            "   BTC Down Token Price: ${:.4}",
            opportunity.btc_down_price
        );
        info!(
            "   Total Cost: ${:.4}",
            opportunity.total_cost
        );
        info!(
            "   Expected Profit: ${:.4} ({:.2}%)",
            opportunity.expected_profit,
            (opportunity.expected_profit / opportunity.total_cost) * Decimal::from(100)
        );
        info!(
            "   ETH Token ID: {}",
            opportunity.eth_up_token_id
        );
        info!(
            "   BTC Token ID: {}",
            opportunity.btc_down_token_id
        );

        // Calculate position size (total dollar amount to invest)
        // For simulation, use default min_order_size = 5.0
        let min_order_size = 5.0; // Default lote mínimo
        let position_size = self.calculate_position_size(opportunity, min_order_size);
        info!("   Position Size: ${:.2} (total investment amount)", position_size);
        
        // Calculate how many units we're buying
        let cost_per_unit = f64::try_from(opportunity.total_cost).unwrap_or(1.0);
        let units = position_size / cost_per_unit;
        info!("   Units: {:.2} (each unit = ${:.4}, so ${:.2} / ${:.4} = {:.2} units)", 
              units, cost_per_unit, position_size, cost_per_unit, units);
        info!("   ETH Up amount: ${:.2} ({} units × ${:.4})", 
              units * f64::try_from(opportunity.eth_up_price).unwrap_or(0.0),
              units, opportunity.eth_up_price);
        info!("   BTC Down amount: ${:.2} ({} units × ${:.4})", 
              units * f64::try_from(opportunity.btc_down_price).unwrap_or(0.0),
              units, opportunity.btc_down_price);

        // In simulation mode, we track the trade and will calculate actual profit when markets close
        // Use condition IDs as key - only ONE trade per period (no accumulation)
        let trade_key = format!("{}_{}", opportunity.eth_condition_id, opportunity.btc_condition_id);
        
        let mut pending = self.pending_trades.lock().await;
        
        // If we already have a trade for this period, skip this opportunity
        if pending.contains_key(&trade_key) {
            drop(pending);
            info!("⏭️  Skipping trade: Already have a pending trade for this period (max_position_size limit)");
            return Ok(());
        }
        
        // First trade for this period - create new entry (no accumulation)
        let pending_trade = PendingTrade {
            eth_token_id: opportunity.eth_up_token_id.clone(),
            btc_token_id: opportunity.btc_down_token_id.clone(),
            eth_condition_id: opportunity.eth_condition_id.clone(),
            btc_condition_id: opportunity.btc_condition_id.clone(),
            investment_amount: position_size,
            units,
            timestamp: std::time::Instant::now(),
        };
        pending.insert(trade_key, pending_trade);
        drop(pending);
        
        let mut trades = self.trades_executed.lock().await;
        *trades += 1;
        let trades_count = *trades;
        drop(trades);

        info!(
            "   ✅ Simulated Trade Executed - Investment: ${:.2} | Expected Profit: ${:.4} | Trades: {}",
            position_size,
            f64::try_from(opportunity.expected_profit).unwrap_or(0.0) * units,
            trades_count
        );

        Ok(())
    }

    async fn execute_real_trade(&self, opportunity: &ArbitrageOpportunity) -> Result<()> {
        // Check if we already have a pending trade for this period
        let trade_key = format!("{}_{}", opportunity.eth_condition_id, opportunity.btc_condition_id);
        let mut pending = self.pending_trades.lock().await;
        
        // Count how many trades are currently open
        let current_trades_count = pending.len() as u32;
        
        // If we already have max_concurrent_trades, skip this opportunity
        if current_trades_count >= self.config.max_concurrent_trades {
            drop(pending);
            info!("⏭️  Skipping trade: Already have {} trades open (max: {})", 
                  current_trades_count, self.config.max_concurrent_trades);
            return Ok(());
        }
        
        // If we already have a trade for this specific period, skip
        if pending.contains_key(&trade_key) {
            drop(pending);
            info!("⏭️  Skipping trade: Already have a pending trade for this period");
            return Ok(());
        }
        drop(pending);
        
        info!("🚀 PRODUCTION: Executing real arbitrage trade...");
        
        // Try to get minimum_order_size from market (may be 5, not 0.01)
        // IMPORTANT: Lote mínimo real do Polymarket geralmente é 5 unidades, não 0.01!
        let min_order_size = self.get_minimum_order_size(&opportunity.eth_condition_id).await
            .unwrap_or(5.0); // Default to 5 if we can't get it from market
        
        // Calculate position size (divides total balance by max_concurrent_trades)
        let position_size = self.calculate_position_size(opportunity, min_order_size);
        
        // Skip if position size is too small (can't afford even one lot)
        if position_size < 0.01 {
            info!("⏭️  Skipping trade: Position size too small (${:.2})", position_size);
            return Ok(());
        }
        
        let cost_per_unit = f64::try_from(opportunity.total_cost).unwrap_or(1.0);
        
        // Calculate how many minimum lots we can buy
        // Each lot costs: min_order_size * cost_per_unit
        // Example: min_order_size = 5, cost_per_unit = $0.97, each lot = $4.85
        let cost_per_min_lot = min_order_size * cost_per_unit;
        
        if cost_per_min_lot > position_size {
            info!("⏭️  Skipping trade: Cannot afford minimum lot. Position size = ${:.2}, Min lot cost = ${:.2}", 
                  position_size, cost_per_min_lot);
            return Ok(());
        }
        
        // Calculate number of lots (always 1 lot minimum for now)
        let min_lots = (position_size / cost_per_min_lot).floor().max(1.0);
        
        // Size is the number of units (lots * min_order_size)
        let units = min_lots * min_order_size;
        let size_str = format!("{:.6}", units);
        
        // Calculate actual position size used
        let actual_position_size = units * cost_per_unit;
        
        info!("💰 Trade details: Max per trade = ${:.2}, Position size = ${:.2}, Min order size = {}, Lots = {:.0}, Units = {:.2}, Cost per unit = ${:.4}", 
              position_size, actual_position_size, min_order_size, min_lots, units, cost_per_unit);

        // Determine order type and price based on config
        let (order_type, eth_price, btc_price, post_only) = if self.config.use_market_orders {
            // MARKET orders: execução garantida, sem preço específico
            ("MARKET".to_string(), "0".to_string(), "0".to_string(), None)
        } else if self.config.post_only {
            // POST_ONLY: melhorar preço para ser maker (preço mais baixo para compra)
            let eth_current = f64::try_from(opportunity.eth_up_price).unwrap_or(0.0);
            let btc_current = f64::try_from(opportunity.btc_down_price).unwrap_or(0.0);
            // Preço mais baixo = melhor para compra (maker)
            let eth_price = (eth_current * (1.0 - self.config.price_improvement_pct / 100.0)).max(0.001);
            let btc_price = (btc_current * (1.0 - self.config.price_improvement_pct / 100.0)).max(0.001);
            ("LIMIT".to_string(), format!("{:.6}", eth_price), format!("{:.6}", btc_price), Some(true))
        } else {
            // LIMIT com preço melhorado para garantir execução como taker
            let eth_current = f64::try_from(opportunity.eth_up_price).unwrap_or(0.0);
            let btc_current = f64::try_from(opportunity.btc_down_price).unwrap_or(0.0);
            // Preço mais alto = melhor para compra (taker, execução garantida)
            let eth_price = eth_current * (1.0 + self.config.price_improvement_pct / 100.0);
            let btc_price = btc_current * (1.0 + self.config.price_improvement_pct / 100.0);
            ("LIMIT".to_string(), format!("{:.6}", eth_price), format!("{:.6}", btc_price), None)
        };
        
        info!("📋 Order config: type={}, post_only={:?}, price_improvement={:.2}%", 
              order_type, post_only, self.config.price_improvement_pct);
        
        // Place order for ETH Up token
        let eth_order = OrderRequest {
            token_id: opportunity.eth_up_token_id.clone(),
            side: "BUY".to_string(),
            size: size_str.clone(),
            price: eth_price,
            order_type: order_type.clone(),
            post_only,
        };

        // Place order for BTC Down token
        let btc_order = OrderRequest {
            token_id: opportunity.btc_down_token_id.clone(),
            side: "BUY".to_string(),
            size: size_str.clone(),
            price: btc_price,
            order_type,
            post_only,
        };

        // Execute both orders
        let is_production = !self.simulation_mode;
        let (eth_result, btc_result) = tokio::join!(
            self.api.place_order(&eth_order, is_production),
            self.api.place_order(&btc_order, is_production)
        );

        match eth_result {
            Ok(response) => {
                info!("✅ ETH Up order placed: {:?}", response);
            }
            Err(e) => {
                warn!("❌ Failed to place ETH Up order: {}", e);
                return Err(e);
            }
        }

        match btc_result {
            Ok(response) => {
                info!("✅ BTC Down order placed: {:?}", response);
            }
            Err(e) => {
                warn!("❌ Failed to place BTC Down order: {}", e);
                return Err(e);
            }
        }

        // Track the trade so we can sell tokens when markets close
        // Only create ONE trade per period (no accumulation)
        let mut pending = self.pending_trades.lock().await;
        let pending_trade = PendingTrade {
            eth_token_id: opportunity.eth_up_token_id.clone(),
            btc_token_id: opportunity.btc_down_token_id.clone(),
            eth_condition_id: opportunity.eth_condition_id.clone(),
            btc_condition_id: opportunity.btc_condition_id.clone(),
            investment_amount: actual_position_size,
            units,
            timestamp: std::time::Instant::now(),
        };
        pending.insert(trade_key, pending_trade);
        drop(pending);
        
        let mut trades = self.trades_executed.lock().await;
        *trades += 1;
        let trades_count = *trades;
        drop(trades);

        info!(
            "✅ Real Trade Executed - Investment: ${:.2} | Units: {:.2} | Expected Profit: ${:.4} | Trades: {}",
            actual_position_size,
            units,
            f64::try_from(opportunity.expected_profit).unwrap_or(0.0) * units,
            trades_count
        );

        Ok(())
    }

    /// Get minimum_order_size from market (may be 5, not 0.01)
    async fn get_minimum_order_size(&self, condition_id: &str) -> Option<f64> {
        // Try to get from cache first
        {
            let cache = self.market_cache.lock().await;
            if let Some(cached) = cache.get(condition_id) {
                if cached.cached_at.elapsed().as_secs() < 60 {
                    return Some(f64::try_from(cached.market.minimum_order_size).ok()?);
                }
            }
        }
        
        // Cache miss - fetch from API
        match self.api.get_market(condition_id).await {
            Ok(market) => {
                // Update cache
                let mut cache = self.market_cache.lock().await;
                cache.insert(condition_id.to_string(), CachedMarketData {
                    market: market.clone(),
                    cached_at: Instant::now(),
                });
                drop(cache);
                
                f64::try_from(market.minimum_order_size).ok()
            }
            Err(_) => None,
        }
    }

    fn calculate_position_size(&self, opportunity: &ArbitrageOpportunity, min_order_size: f64) -> f64 {
        // 🔥 DIVIDIR O SALDO POR NÚMERO DE TRADES PLANEJADOS
        let total_balance = self.config.max_position_size; // Saldo total ($5.00)
        let max_concurrent_trades = self.config.max_concurrent_trades as f64; // Quantos trades simultâneos
        
        // Dividir saldo entre trades: $5.00 / 3 = $1.67 por trade (se max_concurrent_trades = 3)
        let max_per_trade = total_balance / max_concurrent_trades;
        
        let cost_per_unit = f64::try_from(opportunity.total_cost).unwrap_or(1.0);
        
        // Calcular custo por lote mínimo
        // Exemplo: min_order_size = 5, cost_per_unit = $0.97, cada lote = 5 × $0.97 = $4.85
        let cost_per_min_lot = min_order_size * cost_per_unit;
        
        // Se não podemos comprar nem 1 lote mínimo, retornar 0
        if cost_per_min_lot > max_per_trade {
            return 0.0;
        }
        
        // Calcular quantos lotes mínimos podemos comprar
        let min_lots = (max_per_trade / cost_per_min_lot).floor();
        
        // O tamanho da posição é: número de lotes × custo por lote
        let position_size = min_lots * cost_per_min_lot;
        
        // Garantir que não excedemos max_per_trade
        let position_size = position_size.min(max_per_trade);
        
        // Exemplo com total_balance = $5.0, max_concurrent_trades = 3, min_order_size = 5, cost_per_unit = $0.97:
        // - max_per_trade = $5.0 / 3 = $1.67
        // - cost_per_min_lot = 5 × $0.97 = $4.85
        // - Como $4.85 > $1.67, não podemos fazer 3 trades simultâneos
        // - Neste caso, retornaria 0.0 (precisa ajustar max_concurrent_trades ou aumentar saldo)
        position_size
    }

    pub async fn get_stats(&self) -> (f64, u64) {
        let total = *self.total_profit.lock().await;
        let trades = *self.trades_executed.lock().await;
        (total, trades)
    }
}

