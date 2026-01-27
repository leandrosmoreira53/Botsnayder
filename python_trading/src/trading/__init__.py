"""
Trading module for Polymarket CLOB API.

This module provides real trading functionality using L1 authentication
(private key + funder) to submit limit orders to Polymarket.

Components:
    - PolymarketClient: CLOB API client with L1 authentication
    - OrderManager: Limit order creation and management
    - AllowanceManager: Token approval handling

Usage:
    from src.trading import PolymarketClient, OrderManager

    # Initialize client with L1 auth
    client = PolymarketClient()

    # Create order manager
    orders = OrderManager(client)

    # Submit limit order
    result = orders.submit_limit_order(
        token_id="token_id_here",
        side="BUY",
        price=0.45,
        size=100
    )
"""

from .polymarket_client import PolymarketClient
from .order_manager import OrderManager
from .allowance_manager import AllowanceManager

__all__ = [
    "PolymarketClient",
    "OrderManager",
    "AllowanceManager",
]
