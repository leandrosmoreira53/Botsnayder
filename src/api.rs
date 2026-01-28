use crate::models::{*, AssetType, BalanceAllowanceResponse};
use anyhow::{Context, Result};
use reqwest::Client;
use serde_json::Value;
use std::collections::HashMap;
use std::str::FromStr;

pub struct PolymarketApi {
    client: Client,
    gamma_url: String,
    clob_url: String,
    auth_method: u8, // 0 = API Key Credentials, 1 = Assinatura
    api_key: Option<String>,
    api_secret: Option<String>,
    api_passphrase: Option<String>,
    private_key: Option<String>,
    wallet_address: Option<String>,
}

impl PolymarketApi {
    pub fn new(
        gamma_url: String,
        clob_url: String,
        auth_method: u8,
        api_key: Option<String>,
        api_secret: Option<String>,
        api_passphrase: Option<String>,
        private_key: Option<String>,
        wallet_address: Option<String>,
    ) -> Self {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .expect("Failed to create HTTP client");
        
        Self {
            client,
            gamma_url,
            clob_url,
            auth_method,
            api_key,
            api_secret,
            api_passphrase,
            private_key,
            wallet_address,
        }
    }

    /// Derive signer address from private_key
    fn derive_signer_address(&self) -> Result<String> {
        use ethers::prelude::*;
        use hex;
        
        let private_key = self.private_key.as_ref()
            .ok_or_else(|| anyhow::anyhow!("Private key not configured"))?;
        
        // Remove 0x prefix if present
        let key_hex = private_key.strip_prefix("0x").unwrap_or(private_key);
        let key_bytes = hex::decode(key_hex)
            .context("Failed to decode private_key as hex")?;
        
        if key_bytes.len() != 32 {
            anyhow::bail!("Private key must be 32 bytes (64 hex characters)");
        }
        
        // Convert Vec<u8> to array [u8; 32]
        let mut key_array = [0u8; 32];
        key_array.copy_from_slice(&key_bytes[..32]);
        
        // Create wallet from private key
        let wallet = LocalWallet::from_bytes(&key_array)
            .context("Failed to create wallet from private_key")?;
        
        // Get address
        let address = wallet.address();
        
        Ok(format!("{:?}", address))
    }

    /// Assina uma mensagem usando a chave privada (para autenticação por assinatura)
    /// Formato: Ethereum Signed Message prefix + mensagem
    fn sign_message(&self, message: &str) -> Result<String> {
        use k256::ecdsa::{SigningKey, signature::Signer, Signature};
        use sha3::{Keccak256, Digest};
        use hex;
        
        let private_key = self.private_key.as_ref()
            .ok_or_else(|| anyhow::anyhow!("Private key not configured"))?;
        
        // Remove o prefixo 0x se presente
        let key_hex = private_key.strip_prefix("0x").unwrap_or(private_key);
        let key_bytes = hex::decode(key_hex)
            .context("Failed to decode private key")?;
        
        if key_bytes.len() != 32 {
            anyhow::bail!("Private key must be 32 bytes (64 hex characters)");
        }
        
        // Converter Vec<u8> para array [u8; 32]
        let mut key_array = [0u8; 32];
        key_array.copy_from_slice(&key_bytes[..32]);
        
        let signing_key = SigningKey::from_bytes(&key_array.into())
            .context("Failed to create signing key")?;
        
        // Adicionar prefixo Ethereum Signed Message (padrão para mensagens assinadas)
        let prefix = format!("\x19Ethereum Signed Message:\n{}", message.len());
        let prefixed_message = format!("{}{}", prefix, message);
        
        // Hash da mensagem prefixada usando Keccak256 (padrão Ethereum)
        let mut hasher = Keccak256::new();
        hasher.update(prefixed_message.as_bytes());
        let message_hash = hasher.finalize();
        
        // Assinar o hash
        let signature: Signature = signing_key.sign(&message_hash[..]);
        let sig_bytes = signature.to_bytes();
        
        // Converter para hex com prefixo 0x
        Ok(format!("0x{}", hex::encode(sig_bytes)))
    }

    /// Get all active markets (using events endpoint)
    pub async fn get_all_active_markets(&self, limit: u32) -> Result<Vec<Market>> {
        let url = format!("{}/events", self.gamma_url);
        let limit_str = limit.to_string();
        let mut params = HashMap::new();
        params.insert("active", "true");
        params.insert("closed", "false");
        params.insert("limit", &limit_str);

        let response = self
            .client
            .get(&url)
            .query(&params)
            .send()
            .await
            .context("Failed to fetch all active markets")?;

        let status = response.status();
        let json: Value = response.json().await.context("Failed to parse markets response")?;
        
        if !status.is_success() {
            log::warn!("Get all active markets API returned error status {}: {}", status, serde_json::to_string(&json).unwrap_or_default());
            anyhow::bail!("API returned error status {}: {}", status, serde_json::to_string(&json).unwrap_or_default());
        }
        
        // Extract markets from events - events contain markets
        let mut all_markets = Vec::new();
        
        if let Some(events) = json.as_array() {
            for event in events {
                if let Some(markets) = event.get("markets").and_then(|m| m.as_array()) {
                    for market_json in markets {
                        if let Ok(market) = serde_json::from_value::<Market>(market_json.clone()) {
                            all_markets.push(market);
                        }
                    }
                }
            }
        } else if let Some(data) = json.get("data") {
            if let Some(events) = data.as_array() {
                for event in events {
                    if let Some(markets) = event.get("markets").and_then(|m| m.as_array()) {
                        for market_json in markets {
                            if let Ok(market) = serde_json::from_value::<Market>(market_json.clone()) {
                                all_markets.push(market);
                            }
                        }
                    }
                }
            }
        }
        
        log::debug!("Fetched {} active markets from events endpoint", all_markets.len());
        Ok(all_markets)
    }

    /// Get market by slug (e.g., "btc-updown-15m-1767726000")
    /// The API returns an event object with a markets array
    pub async fn get_market_by_slug(&self, slug: &str) -> Result<Market> {
        let url = format!("{}/events/slug/{}", self.gamma_url, slug);
        
        let response = self.client.get(&url).send().await
            .context(format!("Failed to fetch market by slug: {}", slug))?;
        
        let status = response.status();
        if !status.is_success() {
            anyhow::bail!("Failed to fetch market by slug: {} (status: {})", slug, status);
        }
        
        let json: Value = response.json().await
            .context("Failed to parse market response")?;
        
        // The response is an event object with a "markets" array
        // Extract the first market from the markets array
        if let Some(markets) = json.get("markets").and_then(|m| m.as_array()) {
            if let Some(market_json) = markets.first() {
                // Try to deserialize the market
                if let Ok(market) = serde_json::from_value::<Market>(market_json.clone()) {
                    return Ok(market);
                }
            }
        }
        
        anyhow::bail!("Invalid market response format: no markets array found")
    }

    /// Get order book for a specific token
    pub async fn get_orderbook(&self, token_id: &str) -> Result<OrderBook> {
        let url = format!("{}/book", self.clob_url);
        let params = [("token_id", token_id)];

        let response = self
            .client
            .get(&url)
            .query(&params)
            .send()
            .await
            .context("Failed to fetch orderbook")?;

        let orderbook: OrderBook = response
            .json()
            .await
            .context("Failed to parse orderbook")?;

        Ok(orderbook)
    }

    /// Get market details by condition ID
    pub async fn get_market(&self, condition_id: &str) -> Result<MarketDetails> {
        let url = format!("{}/markets/{}", self.clob_url, condition_id);

        let response = self
            .client
            .get(&url)
            .send()
            .await
            .context(format!("Failed to fetch market for condition_id: {}", condition_id))?;

        let status = response.status();
        
        if !status.is_success() {
            anyhow::bail!("Failed to fetch market (status: {})", status);
        }

        let json_text = response.text().await
            .context("Failed to read response body")?;

        let market: MarketDetails = serde_json::from_str(&json_text)
            .map_err(|e| {
                log::error!("Failed to parse market response: {}. Response was: {}", e, json_text);
                anyhow::anyhow!("Failed to parse market response: {}", e)
            })?;

        log::info!("Market response: condition_id={}, active={}, closed={}, accepting_orders={}, tokens={}", 
                  market.condition_id, market.active, market.closed, market.accepting_orders, market.tokens.len());
        
        for token in &market.tokens {
            log::info!("  Token: outcome={}, price={}, token_id={}, winner={}", 
                      token.outcome, token.price, token.token_id, token.winner);
        }

        Ok(market)
    }

    /// Get price for a token (for trading)
    /// side: "BUY" or "SELL"
    pub async fn get_price(&self, token_id: &str, side: &str) -> Result<rust_decimal::Decimal> {
        let url = format!("{}/price", self.clob_url);
        let params = [
            ("side", side),
            ("token_id", token_id),
        ];

        log::debug!("Fetching price from: {}?side={}&token_id={}", url, side, token_id);

        let response = self
            .client
            .get(&url)
            .query(&params)
            .send()
            .await
            .context("Failed to fetch price")?;

        let status = response.status();
        if !status.is_success() {
            anyhow::bail!("Failed to fetch price (status: {})", status);
        }

        let json: serde_json::Value = response
            .json()
            .await
            .context("Failed to parse price response")?;

        let price_str = json.get("price")
            .and_then(|p| p.as_str())
            .ok_or_else(|| anyhow::anyhow!("Invalid price response format"))?;

        let price = rust_decimal::Decimal::from_str(price_str)
            .context(format!("Failed to parse price: {}", price_str))?;

        log::debug!("Price for token {} (side={}): {}", token_id, side, price);

        Ok(price)
    }

    /// Get best bid/ask prices for a token (from orderbook)
    pub async fn get_best_price(&self, token_id: &str) -> Result<Option<TokenPrice>> {
        let orderbook = self.get_orderbook(token_id).await?;
        
        let best_bid = orderbook.bids.first().map(|b| b.price);
        let best_ask = orderbook.asks.first().map(|a| a.price);

        if best_ask.is_some() {
            Ok(Some(TokenPrice {
                token_id: token_id.to_string(),
                bid: best_bid,
                ask: best_ask,
            }))
        } else {
            Ok(None)
        }
    }

    /// Place an order (for production mode) - LIMIT ORDER
    ///
    /// Uses L2 API Key authentication (HMAC-SHA256) with POLY_* headers.
    /// CRITICAL: Requires valid API credentials (api_key, api_secret, api_passphrase)
    /// derived from L1 using derive_api_keys.rs script.
    pub async fn place_order(&self, order: &OrderRequest, is_production: bool) -> Result<OrderResponse> {
        // CRITICAL: Endpoint is /order (singular), NOT /orders
        let url = format!("{}/order", self.clob_url);

        // Build order JSON with CORRECT field names (camelCase as per Polymarket API)
        let mut order_json = serde_json::json!({
            "tokenID": order.token_id,  // camelCase, not token_id
            "side": order.side,
            "size": order.size,
            "price": order.price,
            "type": order.order_type,
        });

        // Add postOnly if specified (camelCase, not post_only)
        if let Some(post_only) = order.post_only {
            if post_only {
                order_json["postOnly"] = serde_json::json!(true);
            }
        }

        // Serialize order to JSON string for HMAC signing
        let order_body = serde_json::to_string(&order_json)
            .context("Failed to serialize order")?;

        log::debug!("📋 Order JSON: {}", serde_json::to_string_pretty(&order_json).unwrap_or_default());

        // Timestamp for authentication
        let timestamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs()
            .to_string();

        // CRITICAL: Use API Key authentication (L2) with POLY_* headers
        // This is the ONLY supported method for POST /order
        if self.auth_method == 0 {
            // API Key (L2) authentication - HMAC-SHA256
            let api_key = self.api_key.as_ref()
                .ok_or_else(|| anyhow::anyhow!("API key not configured. Run derive_api_keys.rs first!"))?;
            let api_secret = self.api_secret.as_ref()
                .ok_or_else(|| anyhow::anyhow!("API secret not configured. Run derive_api_keys.rs first!"))?;
            let api_passphrase = self.api_passphrase.as_ref()
                .ok_or_else(|| anyhow::anyhow!("API passphrase not configured. Run derive_api_keys.rs first!"))?;

            // Generate HMAC signature with correct format
            let method = "POST";
            let path = "/order";
            let hmac_signature = self.generate_hmac_signature_l2(&timestamp, method, path, &order_body, api_secret)?;

            log::info!("🔐 Auth: L2 API Key (HMAC-SHA256)");
            log::info!("   API Key: {}...{}", &api_key[..8.min(api_key.len())], &api_key[api_key.len().saturating_sub(4)..]);
            log::info!("📤 Placing LIMIT order: tokenID={}, side={}, price={}, size={}, postOnly={:?}",
                      order.token_id, order.side, order.price, order.size, order.post_only);

            // Build request with POLY_* headers (CRITICAL: not X-* or CLOB_*)
            let response = self.client
                .post(&url)
                .header("Content-Type", "application/json")
                .header("POLY_API_KEY", api_key)
                .header("POLY_PASSPHRASE", api_passphrase)
                .header("POLY_SIGNATURE", &hmac_signature)
                .header("POLY_TIMESTAMP", &timestamp)
                .body(order_body)
                .send()
                .await
                .context("Failed to place order")?;

            return self.handle_order_response(response, is_production).await;

        } else {
            // auth_method == 1: L1 signature (EIP-712)
            // NOTE: L1 auth is more complex and may not work for all orders
            // Recommended to use L2 API keys for order submission
            log::warn!("⚠️  Using L1 signature auth. Consider using L2 API keys (auth_method=0) for better reliability.");

            let wallet_address = self.wallet_address.as_ref()
                .ok_or_else(|| anyhow::anyhow!("funder_address not configured"))?;

            // For L1 auth, we need to sign the order with EIP-712
            let nonce = uuid::Uuid::new_v4().to_string();

            // Create EIP-712 signature
            let message = format!("{}POST/order{}", timestamp, order_body);
            let eip712_signature = self.sign_message(&message)
                .context("Failed to sign EIP-712 message")?;

            log::info!("🔐 Auth: L1 EIP-712 Signature");
            log::info!("   Address: {}", wallet_address);
            log::info!("📤 Placing LIMIT order: tokenID={}, side={}, price={}, size={}, postOnly={:?}",
                      order.token_id, order.side, order.price, order.size, order.post_only);

            // L1 headers use POLY_* format
            let response = self.client
                .post(&url)
                .header("Content-Type", "application/json")
                .header("POLY_ADDRESS", wallet_address)
                .header("POLY_SIGNATURE", &eip712_signature)
                .header("POLY_TIMESTAMP", &timestamp)
                .header("POLY_NONCE", &nonce)
                .body(order_body)
                .send()
                .await
                .context("Failed to place order")?;

            return self.handle_order_response(response, is_production).await;
        }
    }

    /// Handle order response and extract result
    async fn handle_order_response(&self, response: reqwest::Response, is_production: bool) -> Result<OrderResponse> {
        log::debug!("📥 Response status: {}", response.status());
        log::debug!("📥 Response headers: {:?}", response.headers());

        let status_code = response.status().as_u16();

        // Capture raw response text for debugging
        let response_text = response.text().await
            .context("Failed to read response body")?;

        // HARD STOP: Se receber 401 em produção, parar o bot imediatamente
        if status_code == 401 && is_production {
            log::error!("❌ CRITICAL: 401 Unauthorized");
            log::error!("Response: {}", response_text);
            log::error!("");
            log::error!("🛑 STOPPING BOT to prevent spam and rate limiting.");
            log::error!("");
            log::error!("Possíveis causas:");
            log::error!("  - API credentials inválidas (api_key, api_secret, api_passphrase)");
            log::error!("  - Credentials expiradas - execute derive_api_keys.rs novamente");
            log::error!("  - HMAC signature incorreta");
            log::error!("  - Headers incorretos (devem ser POLY_*, não X-* ou CLOB_*)");
            std::process::exit(1);
        }

        // Tratar erros ANTES de tentar parsear como OrderResponse
        if status_code >= 400 {
            // Tentar parsear como ApiError primeiro
            if let Ok(api_error) = serde_json::from_str::<ApiError>(&response_text) {
                anyhow::bail!("CLOB API error ({}): {}", status_code, api_error.error);
            }
            // Se não for JSON de erro, retornar o texto bruto
            anyhow::bail!("CLOB API error ({}): {}", status_code, response_text);
        }

        // Se status é sucesso, tentar parsear como OrderResponse
        let order_response: OrderResponse = serde_json::from_str(&response_text)
            .context(format!("Failed to parse order response. Status: {}, Body: {}", status_code, response_text))?;

        log::info!("✅ Order placed successfully: {:?}", order_response);
        Ok(order_response)
    }

    /// Get account balance
    pub async fn get_balance(&self) -> Result<BalanceResponse> {
        // Try different possible endpoints
        // Polymarket CLOB API might use different endpoints
        let mut endpoints = vec![
            format!("{}/v1/balance", self.clob_url),
            format!("{}/v1/balances", self.clob_url),
            format!("{}/balance", self.clob_url),
            format!("{}/balances", self.clob_url),
            format!("{}/account/balance", self.clob_url),
            format!("{}/account", self.clob_url),
        ];
        
        // If wallet address is available, try endpoints with it
        if let Some(addr) = &self.wallet_address {
            endpoints.extend(vec![
                format!("{}/balance/{}", self.clob_url, addr),
                format!("{}/balances/{}", self.clob_url, addr),
                format!("{}/account/{}/balance", self.clob_url, addr),
            ]);
        }
        
        let mut last_error: Option<String> = None;
        
        for url in &endpoints {
            eprintln!("🔍 Trying endpoint: {}", url);
            
            let mut request = self.client.get(url);
            
            // Escolher método de autenticação baseado em auth_method
            if self.auth_method == 1 {
                // Método 1: Autenticação por assinatura (Signature)
                if let Some(_) = &self.private_key {
                    // Para GET requests, geralmente assinamos o timestamp ou uma mensagem simples
                    let timestamp = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap()
                        .as_secs();
                    let path = url.replace(&self.clob_url, "");
                    let message = format!("GET {} {}", path, timestamp);
                    let signature = match self.sign_message(&message) {
                        Ok(sig) => sig,
                        Err(e) => {
                            last_error = Some(format!("Failed to sign message: {}", e));
                            eprintln!("❌ {}", last_error.as_ref().unwrap());
                            continue;
                        }
                    };
                    
                    if let Some(addr) = &self.wallet_address {
                        request = request.header("X-Wallet-Address", addr);
                    }
                    request = request.header("X-Signature", &signature);
                    request = request.header("X-Timestamp", timestamp.to_string());
                } else {
                    anyhow::bail!("Auth method is set to signature (1) but private_key is not configured");
                }
            } else {
                // Método 0: Autenticação por API Key (Bearer token)
                if let Some(key) = &self.api_key {
                    request = request.header("Authorization", format!("Bearer {}", key));
                } else {
                    anyhow::bail!("Auth method is set to API key (0) but api_key is not configured");
                }
            }

            let response = match request.send().await {
                Ok(resp) => resp,
                Err(e) => {
                    last_error = Some(format!("Network error: {}", e));
                    eprintln!("❌ {} - Trying next endpoint...", last_error.as_ref().unwrap());
                    continue;
                }
            };

            let status = response.status();
            
            // Get response text first to debug
            let response_text = match response.text().await {
                Ok(text) => text,
                Err(e) => {
                    last_error = Some(format!("Failed to read response body: {}", e));
                    eprintln!("❌ {} - Trying next endpoint...", last_error.as_ref().unwrap());
                    continue;
                }
            };
            
            eprintln!("📡 Response status: {}", status);
            eprintln!("📄 Response body: {}", response_text);
            
            if !status.is_success() {
                last_error = Some(format!("Endpoint {} returned status {}: {}", url, status, response_text));
                eprintln!("❌ {} - Trying next endpoint...", last_error.as_ref().unwrap());
                continue; // Try next endpoint
            }
            
            // Try to parse as JSON
            match serde_json::from_str::<BalanceResponse>(&response_text) {
                Ok(balance_response) => {
                    eprintln!("✅ Successfully parsed balance from {}", url);
                    return Ok(balance_response);
                }
                Err(e) => {
                    eprintln!("⚠️  Failed to parse response from {}: {}", url, e);
                    eprintln!("📄 Raw response: {}", response_text);
                    // Try to parse as generic JSON to see structure
                    if let Ok(json) = serde_json::from_str::<serde_json::Value>(&response_text) {
                        eprintln!("📊 JSON structure:");
                        eprintln!("{}", serde_json::to_string_pretty(&json).unwrap_or_default());
                    }
                    last_error = Some(format!("Failed to parse response: {}", e));
                    continue; // Try next endpoint
                }
            }
        }
        
        anyhow::bail!("Failed to get balance from all endpoints. Last error: {}", 
            last_error.unwrap_or_else(|| "Unknown error".to_string()))
    }

    /// Generate HMAC signature for API key authentication (similar to TypeScript ClobClient)
    /// NOTE: This is the OLD method - use generate_hmac_signature_l2 for correct L2 auth
    fn generate_hmac_signature(&self, timestamp: &str, method: &str, path: &str, body: &str) -> Result<String> {
        use hmac::{Hmac, Mac};
        use sha2::Sha256;
        use base64::{Engine as _, engine::general_purpose};

        let secret = self.api_secret.as_ref()
            .ok_or_else(|| anyhow::anyhow!("API secret not configured"))?;

        // Create message to sign: timestamp + method + path + body
        let message = format!("{}{}{}{}", timestamp, method, path, body);

        // Create HMAC-SHA256
        let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes())
            .context("Failed to create HMAC")?;
        mac.update(message.as_bytes());
        let result = mac.finalize();
        let signature = general_purpose::STANDARD.encode(result.into_bytes());

        Ok(signature)
    }

    /// Generate HMAC signature for L2 API key authentication
    /// CRITICAL: The secret must be base64 DECODED before use as HMAC key!
    /// Message format: timestamp + method + path + body
    fn generate_hmac_signature_l2(&self, timestamp: &str, method: &str, path: &str, body: &str, secret: &str) -> Result<String> {
        use hmac::{Hmac, Mac};
        use sha2::Sha256;
        use base64::{Engine as _, engine::general_purpose};

        // CRITICAL: The API secret is base64-encoded, we must decode it first!
        let secret_bytes = general_purpose::STANDARD.decode(secret)
            .context("Failed to decode API secret from base64. Ensure it's valid base64.")?;

        // Create message to sign: timestamp + method + path + body
        let message = format!("{}{}{}{}", timestamp, method, path, body);

        log::debug!("HMAC message: {}", message);

        // Create HMAC-SHA256 using the DECODED secret bytes
        let mut mac = Hmac::<Sha256>::new_from_slice(&secret_bytes)
            .context("Failed to create HMAC")?;
        mac.update(message.as_bytes());
        let result = mac.finalize();

        // Encode the signature as base64
        let signature = general_purpose::STANDARD.encode(result.into_bytes());

        log::debug!("HMAC signature: {}", signature);

        Ok(signature)
    }

    /// Get balance allowance (similar to TypeScript getBalanceAllowance)
    /// asset_type: COLLATERAL or CONDITIONAL
    /// token_id: Optional token ID for conditional assets
    pub async fn get_balance_allowance(
        &self,
        asset_type: AssetType,
        token_id: Option<&str>,
    ) -> Result<BalanceAllowanceResponse> {
        // Try both endpoints: /balance-allowance (with hyphen) and /balance/allowance (with slash)
        let paths = vec!["/balance-allowance", "/balance/allowance"];
        let method = "GET";
        let body = "";
        
        // Build query parameters
        let mut query_params = vec![
            ("asset_type", asset_type.as_str()),
        ];
        
        if let Some(tid) = token_id {
            query_params.push(("token_id", tid));
        }
        
        let mut last_error: Option<String> = None;
        
        // Try each endpoint
        for path in &paths {
            let url = format!("{}{}", self.clob_url, path);
            let mut request = self.client.get(&url).query(&query_params);
            
            // Authenticate based on auth_method
            if self.auth_method == 0 {
                // API Key Credentials (HMAC authentication)
                let api_key = self.api_key.as_ref()
                    .ok_or_else(|| anyhow::anyhow!("API key not configured"))?;
                let api_secret = self.api_secret.as_ref()
                    .ok_or_else(|| anyhow::anyhow!("API secret not configured"))?;
                let passphrase = self.api_passphrase.as_ref()
                    .ok_or_else(|| anyhow::anyhow!("API passphrase not configured"))?;

                let timestamp = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_secs()
                    .to_string();

                // Use corrected HMAC signature with base64-decoded secret
                let signature = self.generate_hmac_signature_l2(&timestamp, method, path, body, api_secret)?;

                // CRITICAL: Use POLY_* headers, NOT CLOB_*
                request = request
                    .header("POLY_API_KEY", api_key)
                    .header("POLY_PASSPHRASE", passphrase)
                    .header("POLY_SIGNATURE", &signature)
                    .header("POLY_TIMESTAMP", &timestamp);
            } else {
                // Signature authentication (auth_method = 1)
                // Note: Some endpoints may only support API Key auth, not signature
                // If signature auth fails, user may need to use API Key credentials
                if let Some(_) = &self.private_key {
                    let timestamp = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap()
                        .as_secs();
                    // For GET requests, sign the path and timestamp
                    let message = format!("GET {} {}", path, timestamp);
                    let signature = self.sign_message(&message)?;
                    
                    if let Some(addr) = &self.wallet_address {
                        request = request.header("X-Wallet-Address", addr);
                    }
                    request = request.header("X-Signature", &signature);
                    request = request.header("X-Timestamp", timestamp.to_string());
                } else {
                    anyhow::bail!("Auth method is set to signature (1) but private_key is not configured");
                }
            }

            eprintln!("🔍 Trying endpoint: {}", url);
            eprintln!("📋 Query params: asset_type={}, token_id={:?}", 
                asset_type.as_str(), token_id);
            
            let response = match request.send().await {
                Ok(resp) => resp,
                Err(e) => {
                    last_error = Some(format!("Network error: {}", e));
                    eprintln!("❌ {} - Trying next endpoint...", last_error.as_ref().unwrap());
                    continue;
                }
            };

            let status = response.status();
            let response_text = response.text().await
                .context("Failed to read response body")?;

            eprintln!("📡 Response status: {}", status);
            
            if !status.is_success() {
                last_error = Some(format!("Endpoint {} returned status {}: {}", url, status, 
                    if response_text.len() > 200 { 
                        format!("{}...", &response_text[..200]) 
                    } else { 
                        response_text.clone() 
                    }));
                eprintln!("❌ {} - Trying next endpoint...", last_error.as_ref().unwrap());
                continue;
            }

            eprintln!("📄 Response body: {}", 
                if response_text.len() > 500 { 
                    format!("{}...", &response_text[..500]) 
                } else { 
                    response_text.clone() 
                });

            // Try to parse as JSON
            match serde_json::from_str::<BalanceAllowanceResponse>(&response_text) {
                Ok(balance_response) => {
                    eprintln!("✅ Successfully retrieved balance from {}", url);
                    return Ok(balance_response);
                }
                Err(e) => {
                    eprintln!("⚠️  Failed to parse response from {}: {}", url, e);
                    // Try to parse as generic JSON to see structure
                    if let Ok(json) = serde_json::from_str::<serde_json::Value>(&response_text) {
                        eprintln!("📊 JSON structure:");
                        eprintln!("{}", serde_json::to_string_pretty(&json).unwrap_or_default());
                    }
                    last_error = Some(format!("Failed to parse response: {}", e));
                    continue;
                }
            }
        }
        
        anyhow::bail!("Failed to get balance allowance from all endpoints. Last error: {}", 
            last_error.unwrap_or_else(|| "Unknown error".to_string()))
    }

    /// Get balances for a specific address (similar to Python get_balances)
    /// Uses endpoint: GET /balances?user={address}
    /// Based on: https://github.com/lorine93s/polymarket-market-maker-bot/blob/main/src/polymarket/rest_client.py
    /// Note: The Python code uses `polymarket_api_url` which might be gamma-api, not clob-api
    pub async fn get_balances(&self, address: &str) -> Result<serde_json::Value> {
        // Try both gamma_api_url and clob_api_url (Python code uses base_url which could be either)
        let urls = vec![
            format!("{}/balances", self.gamma_url),
            format!("{}/balances", self.clob_url),
        ];
        
        let mut last_error: Option<String> = None;
        
        for url in &urls {
        
            let mut request = self.client.get(url).query(&[("user", address)]);
            
            // Authenticate based on auth_method
            if self.auth_method == 1 {
                // Signature authentication (auth_method = 1)
                if let Some(_) = &self.private_key {
                    let timestamp = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap()
                        .as_secs();
                    let path = "/balances";
                    let message = format!("GET {} {}", path, timestamp);
                    let signature = self.sign_message(&message)?;
                    
                    if let Some(addr) = &self.wallet_address {
                        request = request.header("X-Wallet-Address", addr);
                    }
                    request = request.header("X-Signature", &signature);
                    request = request.header("X-Timestamp", timestamp.to_string());
                } else {
                    last_error = Some("Auth method is set to signature (1) but private_key is not configured".to_string());
                    continue;
                }
            } else {
                // API Key authentication (auth_method = 0)
                if let Some(key) = &self.api_key {
                    request = request.header("Authorization", format!("Bearer {}", key));
                } else {
                    last_error = Some("Auth method is set to API key (0) but api_key is not configured".to_string());
                    continue;
                }
            }

            eprintln!("🔍 Trying endpoint: {}?user={}", url, address);
            
            let response = match request.send().await {
                Ok(resp) => resp,
                Err(e) => {
                    last_error = Some(format!("Network error: {}", e));
                    eprintln!("❌ {} - Trying next URL...", last_error.as_ref().unwrap());
                    continue;
                }
            };

            let status = response.status();
            let response_text = match response.text().await {
                Ok(text) => text,
                Err(e) => {
                    last_error = Some(format!("Failed to read response: {}", e));
                    eprintln!("❌ {} - Trying next URL...", last_error.as_ref().unwrap());
                    continue;
                }
            };

            eprintln!("📡 Response status: {}", status);
            
            if !status.is_success() {
                eprintln!("📄 Response body: {}", 
                    if response_text.len() > 200 { 
                        format!("{}...", &response_text[..200]) 
                    } else { 
                        response_text.clone() 
                    });
                last_error = Some(format!("Get balances failed with status {}: {}", status, 
                    if response_text.len() > 200 { 
                        format!("{}...", &response_text[..200]) 
                    } else { 
                        response_text.clone() 
                    }));
                eprintln!("❌ {} - Trying next URL...", last_error.as_ref().unwrap());
                continue;
            }

            eprintln!("📄 Response body: {}", 
                if response_text.len() > 500 { 
                    format!("{}...", &response_text[..500]) 
                } else { 
                    response_text.clone() 
                });

            // Parse as generic JSON (since the structure may vary)
            match serde_json::from_str::<serde_json::Value>(&response_text) {
                Ok(json) => {
                    eprintln!("✅ Successfully retrieved balances from {}", url);
                    return Ok(json);
                }
                Err(e) => {
                    last_error = Some(format!("Failed to parse response: {}", e));
                    eprintln!("❌ {} - Trying next URL...", last_error.as_ref().unwrap());
                    continue;
                }
            }
        }
        
        anyhow::bail!("Failed to get balances from all URLs. Last error: {}", 
            last_error.unwrap_or_else(|| "Unknown error".to_string()))
    }
}

