use crate::models::*;
use crate::monitor::MarketSnapshot;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

#[derive(Clone)]
pub struct ArbitrageDetector {
    min_profit_threshold: Decimal,
}

impl ArbitrageDetector {
    pub fn new(min_profit_threshold: f64) -> Self {
        Self {
            min_profit_threshold: Decimal::from_f64_retain(min_profit_threshold)
                .unwrap_or(dec!(0.01)),
        }
    }

    /// Detect arbitrage opportunities between ETH and BTC markets
    /// Strategy: Buy Up token in ETH market + Buy Down token in BTC market
    /// when total cost < $1
    pub fn detect_opportunities(&self, snapshot: &MarketSnapshot) -> Vec<ArbitrageOpportunity> {
        let mut opportunities = Vec::new();

        // Get prices from both markets
        let eth_up = snapshot.eth_market.up_token.as_ref();
        let eth_down = snapshot.eth_market.down_token.as_ref();
        let btc_up = snapshot.btc_market.up_token.as_ref();
        let btc_down = snapshot.btc_market.down_token.as_ref();
        
        log::info!("🔍 Checking arbitrage opportunities:");
        if let Some(eth_up_price) = eth_up.as_ref() {
            log::info!("   ETH Up: ask=${}", eth_up_price.ask_price());
        }
        if let Some(eth_down_price) = eth_down.as_ref() {
            log::info!("   ETH Down: ask=${}", eth_down_price.ask_price());
        }
        if let Some(btc_up_price) = btc_up.as_ref() {
            log::info!("   BTC Up: ask=${}", btc_up_price.ask_price());
        }
        if let Some(btc_down_price) = btc_down.as_ref() {
            log::info!("   BTC Down: ask=${}", btc_down_price.ask_price());
        }

        // Strategy 1: ETH Up + BTC Down
        if let (Some(eth_up_price), Some(btc_down_price)) = (eth_up, btc_down) {
            if let Some(opportunity) = self.check_arbitrage(
                eth_up_price,
                btc_down_price,
                &snapshot.eth_market.condition_id,
                &snapshot.btc_market.condition_id,
                "ETH_UP",
                "BTC_DOWN",
            ) {
                opportunities.push(opportunity);
            }
        }

        // Strategy 2: ETH Down + BTC Up
        if let (Some(eth_down_price), Some(btc_up_price)) = (eth_down, btc_up) {
            if let Some(opportunity) = self.check_arbitrage(
                eth_down_price,
                btc_up_price,
                &snapshot.eth_market.condition_id,
                &snapshot.btc_market.condition_id,
                "ETH_DOWN",
                "BTC_UP",
            ) {
                opportunities.push(opportunity);
            }
        }

        opportunities
    }

    fn check_arbitrage(
        &self,
        token1: &TokenPrice,
        token2: &TokenPrice,
        _condition1: &str,
        _condition2: &str,
        _label1: &str,
        _label2: &str,
    ) -> Option<ArbitrageOpportunity> {
        let price1 = token1.ask_price();
        let price2 = token2.ask_price();
        
        // Debug: Log prices being checked
        log::debug!("🔍 Checking arbitrage: {} price={}, {} price={}", 
                   _label1, price1, _label2, price2);
        
        // Check if prices are valid (not zero)
        if price1 == Decimal::ZERO || price2 == Decimal::ZERO {
            log::debug!("⏭️  Skipping: Invalid prices (price1={}, price2={})", price1, price2);
            return None;
        }
        
        let total_cost = price1 + price2;
        let dollar = dec!(1.0);
        let min_price_threshold = dec!(0.6);

        // Safety filter: Don't trade if both tokens are below $0.6 (rug case)
        // This avoids cases where both markets might go against us
        if price1 < min_price_threshold && price2 < min_price_threshold {
            log::debug!("⏭️  Skipping: Both prices below $0.6 (rug protection)");
            return None;
        }

        // Check if total cost is STRICTLY less than $1 (must be < 1.0, not <= 1.0)
        // Use a small epsilon to handle floating point precision
        let epsilon = dec!(0.0001);
        if total_cost < dollar - epsilon {
            let expected_profit = dollar - total_cost;
            
            log::info!("💰 Arbitrage found: {} + {} = ${} (profit: ${}, threshold: ${})", 
                      _label1, _label2, total_cost, expected_profit, self.min_profit_threshold);
            
            // Only return if profit meets threshold
            if expected_profit >= self.min_profit_threshold {
                log::info!("✅ Arbitrage opportunity detected: {} + {} = ${} (profit: ${})", 
                          _label1, _label2, total_cost, expected_profit);
                return Some(ArbitrageOpportunity {
                    eth_up_price: price1,
                    btc_down_price: price2,
                    total_cost,
                    expected_profit,
                    eth_up_token_id: token1.token_id.clone(),
                    btc_down_token_id: token2.token_id.clone(),
                    eth_condition_id: _condition1.to_string(),
                    btc_condition_id: _condition2.to_string(),
                });
            } else {
                log::debug!("⏭️  Skipping: Profit ${} below threshold ${}", 
                           expected_profit, self.min_profit_threshold);
            }
        } else {
            log::debug!("⏭️  Skipping: Total cost ${} >= $1.00 (no arbitrage)", total_cost);
        }

        None
    }
}

