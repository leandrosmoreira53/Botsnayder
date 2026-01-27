"""
Order Manager for Polymarket Trading.

Handles limit order creation, submission, and management
for the delta-neutral spread strategy.
"""

import logging
import time
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
from enum import Enum

from config.trading_config import TradingConfig
from .polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)


class OrderSide(Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    """Order status."""
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class OrderResult:
    """Result of an order submission."""
    success: bool
    order_id: Optional[str] = None
    message: str = ""
    details: Optional[Dict[str, Any]] = None


@dataclass
class SpreadOrder:
    """
    A spread order consisting of paired YES/NO limit orders.

    For delta-neutral strategy, we buy both YES and NO tokens
    when their combined price is below $1.00.
    """
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    size: float
    yes_order_id: Optional[str] = None
    no_order_id: Optional[str] = None
    status: str = "pending"


class OrderManager:
    """
    Manages limit orders for Polymarket trading.

    Handles:
    - Single limit order submission
    - Paired spread orders (YES + NO)
    - Order monitoring and cancellation
    - Position sizing validation
    """

    def __init__(
        self,
        client: PolymarketClient,
        config: Optional[TradingConfig] = None,
    ):
        """
        Initialize order manager.

        Args:
            client: Authenticated PolymarketClient
            config: Trading configuration
        """
        self.client = client
        self.config = config or TradingConfig.from_env()
        self.pending_orders: Dict[str, Dict[str, Any]] = {}
        self.spread_orders: List[SpreadOrder] = []

    def validate_order(
        self,
        price: float,
        size: float,
    ) -> Tuple[bool, str]:
        """
        Validate order parameters.

        Args:
            price: Order price (0-1)
            size: Order size (shares)

        Returns:
            Tuple of (is_valid, error_message)
        """
        if price <= 0 or price >= 1:
            return False, f"Price must be between 0 and 1, got {price}"

        if size < self.config.MIN_ORDER_SIZE:
            return False, f"Size {size} below minimum {self.config.MIN_ORDER_SIZE}"

        if size > self.config.MAX_ORDER_SIZE:
            return False, f"Size {size} above maximum {self.config.MAX_ORDER_SIZE}"

        return True, ""

    def submit_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> OrderResult:
        """
        Submit a single limit order.

        Args:
            token_id: Token ID to trade
            side: "BUY" or "SELL"
            price: Limit price (0-1)
            size: Number of shares
            order_type: "GTC", "GTD", or "FOK"

        Returns:
            OrderResult with success status and order ID
        """
        # Validate
        is_valid, error = self.validate_order(price, size)
        if not is_valid:
            return OrderResult(success=False, message=error)

        try:
            logger.info(
                f"Submitting {side} order: {size} shares @ ${price:.4f} "
                f"(token: {token_id[:16]}...)"
            )

            response = self.client.create_and_submit_order(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                order_type=order_type,
            )

            order_id = response.get("orderID") or response.get("order_id")

            if order_id:
                logger.info(f"Order submitted: {order_id}")
                self.pending_orders[order_id] = {
                    "token_id": token_id,
                    "side": side,
                    "price": price,
                    "size": size,
                    "status": "open",
                    "timestamp": time.time(),
                }
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    message="Order submitted successfully",
                    details=response,
                )
            else:
                return OrderResult(
                    success=False,
                    message="No order ID in response",
                    details=response,
                )

        except Exception as e:
            logger.error(f"Order submission failed: {e}")
            return OrderResult(success=False, message=str(e))

    def submit_spread_order(
        self,
        yes_token_id: str,
        no_token_id: str,
        yes_price: float,
        no_price: float,
        size: float,
        order_type: str = "GTC",
    ) -> Tuple[OrderResult, OrderResult]:
        """
        Submit paired YES/NO orders for delta-neutral spread.

        Args:
            yes_token_id: YES token ID
            no_token_id: NO token ID
            yes_price: Limit price for YES
            no_price: Limit price for NO
            size: Number of shares for each side
            order_type: Order type

        Returns:
            Tuple of (yes_result, no_result)
        """
        total_price = yes_price + no_price
        if total_price >= 1.0:
            return (
                OrderResult(success=False, message=f"No spread opportunity: {total_price:.4f} >= 1.0"),
                OrderResult(success=False, message="Skipped due to no spread"),
            )

        spread = 1.0 - total_price
        logger.info(
            f"Submitting spread order: {size} pairs @ YES=${yes_price:.4f} + NO=${no_price:.4f} "
            f"(spread: {spread:.4f})"
        )

        # Submit both orders
        yes_result = self.submit_limit_order(
            token_id=yes_token_id,
            side="BUY",
            price=yes_price,
            size=size,
            order_type=order_type,
        )

        no_result = self.submit_limit_order(
            token_id=no_token_id,
            side="BUY",
            price=no_price,
            size=size,
            order_type=order_type,
        )

        # Track spread order
        spread_order = SpreadOrder(
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            yes_price=yes_price,
            no_price=no_price,
            size=size,
            yes_order_id=yes_result.order_id,
            no_order_id=no_result.order_id,
            status="open" if (yes_result.success and no_result.success) else "partial",
        )
        self.spread_orders.append(spread_order)

        return yes_result, no_result

    def cancel_order(self, order_id: str) -> OrderResult:
        """
        Cancel an open order.

        Args:
            order_id: Order ID to cancel

        Returns:
            OrderResult with cancellation status
        """
        try:
            logger.info(f"Cancelling order: {order_id}")
            response = self.client.cancel_order(order_id)

            if order_id in self.pending_orders:
                self.pending_orders[order_id]["status"] = "cancelled"

            return OrderResult(
                success=True,
                order_id=order_id,
                message="Order cancelled",
                details=response,
            )

        except Exception as e:
            logger.error(f"Cancel failed: {e}")
            return OrderResult(success=False, message=str(e))

    def cancel_all_orders(self) -> OrderResult:
        """
        Cancel all open orders.

        Returns:
            OrderResult with cancellation status
        """
        try:
            logger.info("Cancelling all orders...")
            response = self.client.cancel_all_orders()

            # Mark all pending as cancelled
            for order_id in self.pending_orders:
                self.pending_orders[order_id]["status"] = "cancelled"

            return OrderResult(
                success=True,
                message="All orders cancelled",
                details=response,
            )

        except Exception as e:
            logger.error(f"Cancel all failed: {e}")
            return OrderResult(success=False, message=str(e))

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Get all open orders from the exchange.

        Returns:
            List of open orders
        """
        return self.client.get_open_orders()

    def get_order_status(self, order_id: str) -> Optional[str]:
        """
        Get status of a specific order.

        Args:
            order_id: Order ID

        Returns:
            Order status string or None
        """
        orders = self.get_open_orders()
        for order in orders:
            if order.get("id") == order_id or order.get("orderID") == order_id:
                return order.get("status", "unknown")

        # If not in open orders, check local cache
        if order_id in self.pending_orders:
            return self.pending_orders[order_id].get("status", "unknown")

        return None

    def calculate_spread_opportunity(
        self,
        yes_token_id: str,
        no_token_id: str,
    ) -> Dict[str, Any]:
        """
        Calculate current spread opportunity for a market.

        Args:
            yes_token_id: YES token ID
            no_token_id: NO token ID

        Returns:
            Dict with spread analysis
        """
        try:
            yes_price = self.client.get_price(yes_token_id, "BUY")
            no_price = self.client.get_price(no_token_id, "BUY")

            if yes_price is None or no_price is None:
                return {
                    "has_opportunity": False,
                    "error": "Could not get prices",
                }

            total_price = yes_price + no_price
            spread = 1.0 - total_price
            has_opportunity = spread > 0.01  # At least 1% spread

            return {
                "has_opportunity": has_opportunity,
                "yes_price": yes_price,
                "no_price": no_price,
                "total_price": total_price,
                "spread": spread,
                "spread_pct": spread * 100,
            }

        except Exception as e:
            return {
                "has_opportunity": False,
                "error": str(e),
            }

    def execute_spread_if_opportunity(
        self,
        yes_token_id: str,
        no_token_id: str,
        size: float,
        min_spread: float = 0.02,
        price_improvement: float = 0.001,
    ) -> Optional[Tuple[OrderResult, OrderResult]]:
        """
        Check for spread opportunity and execute if found.

        Args:
            yes_token_id: YES token ID
            no_token_id: NO token ID
            size: Order size
            min_spread: Minimum spread to execute (default 2%)
            price_improvement: Price improvement for maker orders

        Returns:
            Tuple of order results if executed, None if no opportunity
        """
        opportunity = self.calculate_spread_opportunity(yes_token_id, no_token_id)

        if not opportunity.get("has_opportunity"):
            logger.info(f"No opportunity: {opportunity}")
            return None

        if opportunity["spread"] < min_spread:
            logger.info(
                f"Spread {opportunity['spread']:.4f} below minimum {min_spread}"
            )
            return None

        # Apply price improvement (lower price = better for buyer/maker)
        yes_price = opportunity["yes_price"] - price_improvement
        no_price = opportunity["no_price"] - price_improvement

        # Ensure prices are still valid
        yes_price = max(0.01, yes_price)
        no_price = max(0.01, no_price)

        logger.info(
            f"Executing spread: {opportunity['spread_pct']:.2f}% opportunity "
            f"(YES=${yes_price:.4f}, NO=${no_price:.4f})"
        )

        return self.submit_spread_order(
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            yes_price=yes_price,
            no_price=no_price,
            size=size,
        )

    def print_open_orders(self) -> None:
        """Print all open orders."""
        orders = self.get_open_orders()

        print("\n" + "=" * 60)
        print("OPEN ORDERS")
        print("=" * 60)

        if not orders:
            print("No open orders")
        else:
            for order in orders:
                order_id = order.get("id", order.get("orderID", "unknown"))
                side = order.get("side", "?")
                price = order.get("price", 0)
                size = order.get("original_size", order.get("size", 0))
                filled = order.get("size_matched", 0)
                print(
                    f"  {order_id[:16]}... | {side:4} | "
                    f"${float(price):.4f} | {size} shares | "
                    f"filled: {filled}"
                )

        print("=" * 60)

    def print_spread_orders(self) -> None:
        """Print all spread orders."""
        print("\n" + "=" * 60)
        print("SPREAD ORDERS")
        print("=" * 60)

        if not self.spread_orders:
            print("No spread orders")
        else:
            for i, so in enumerate(self.spread_orders):
                total = so.yes_price + so.no_price
                spread = 1.0 - total
                print(
                    f"  #{i+1} | {so.size} pairs | "
                    f"YES=${so.yes_price:.4f} + NO=${so.no_price:.4f} = ${total:.4f} | "
                    f"spread: {spread:.4f} | status: {so.status}"
                )

        print("=" * 60)
