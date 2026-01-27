"""
Token Allowance Manager for Polymarket Trading.

Before trading on Polymarket, you must approve token spending
for the exchange contracts. This module handles those approvals.

Required approvals:
- USDC: For buying shares
- Conditional Tokens (CTF): For selling shares
"""

import logging
from typing import Optional, Dict, Any, List

from web3 import Web3
from eth_account import Account

from config.trading_config import (
    TradingConfig,
    get_private_key,
    get_funder_address,
    get_rpc_url,
)

logger = logging.getLogger(__name__)

# ERC20 ABI for approve and allowance functions
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]

# Conditional Token Framework ABI for setApprovalForAll
CTF_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]

# Maximum uint256 for unlimited approval
MAX_UINT256 = 2**256 - 1


class AllowanceManager:
    """
    Manages token approvals for Polymarket trading.

    Before you can trade, you must approve the exchange contracts
    to spend your USDC (for buying) and Conditional Tokens (for selling).
    """

    def __init__(
        self,
        private_key: Optional[str] = None,
        config: Optional[TradingConfig] = None,
        rpc_url: Optional[str] = None,
    ):
        """
        Initialize allowance manager.

        Args:
            private_key: Wallet private key
            config: Trading configuration
            rpc_url: Polygon RPC URL
        """
        self.config = config or TradingConfig.from_env()
        self._private_key = private_key or get_private_key()
        self._rpc_url = rpc_url or get_rpc_url()

        # Initialize Web3
        self.w3 = Web3(Web3.HTTPProvider(self._rpc_url))
        if not self.w3.is_connected():
            raise ConnectionError(f"Failed to connect to RPC: {self._rpc_url}")

        # Get account from private key
        self.account = Account.from_key(self._private_key)
        self.address = self.account.address

        # Initialize contracts
        self.usdc_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.config.USDC_ADDRESS),
            abi=ERC20_ABI,
        )
        self.ctf_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.config.CTF_ADDRESS),
            abi=CTF_ABI,
        )

        # Exchange addresses that need approval
        self.exchange_addresses = [
            self.config.EXCHANGE_ADDRESS,
            self.config.NEG_RISK_EXCHANGE_ADDRESS,
            self.config.NEG_RISK_ADAPTER_ADDRESS,
        ]

        logger.info(f"AllowanceManager initialized for {self.address}")

    def get_usdc_balance(self) -> float:
        """
        Get USDC balance.

        Returns:
            USDC balance in human-readable format
        """
        balance_wei = self.usdc_contract.functions.balanceOf(self.address).call()
        # USDC has 6 decimals
        return balance_wei / 1e6

    def get_matic_balance(self) -> float:
        """
        Get MATIC balance for gas.

        Returns:
            MATIC balance
        """
        balance_wei = self.w3.eth.get_balance(self.address)
        return self.w3.from_wei(balance_wei, "ether")

    def check_usdc_allowance(self, spender: str) -> float:
        """
        Check USDC allowance for a spender.

        Args:
            spender: Address to check allowance for

        Returns:
            Allowance amount in human-readable format
        """
        allowance_wei = self.usdc_contract.functions.allowance(
            self.address,
            Web3.to_checksum_address(spender),
        ).call()
        return allowance_wei / 1e6

    def check_ctf_approval(self, operator: str) -> bool:
        """
        Check if CTF is approved for an operator.

        Args:
            operator: Address to check approval for

        Returns:
            True if approved
        """
        return self.ctf_contract.functions.isApprovedForAll(
            self.address,
            Web3.to_checksum_address(operator),
        ).call()

    def check_all_allowances(self) -> Dict[str, Dict[str, Any]]:
        """
        Check all required allowances for trading.

        Returns:
            Dict with allowance status for each exchange contract
        """
        results = {}

        for exchange in self.exchange_addresses:
            exchange_name = self._get_exchange_name(exchange)
            results[exchange_name] = {
                "address": exchange,
                "usdc_allowance": self.check_usdc_allowance(exchange),
                "ctf_approved": self.check_ctf_approval(exchange),
            }

        return results

    def approve_usdc(
        self,
        spender: str,
        amount: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Approve USDC spending for a contract.

        Args:
            spender: Contract address to approve
            amount: Amount to approve (default: unlimited)

        Returns:
            Transaction receipt
        """
        amount = amount or MAX_UINT256

        logger.info(f"Approving USDC for {spender}...")

        # Build transaction
        tx = self.usdc_contract.functions.approve(
            Web3.to_checksum_address(spender),
            amount,
        ).build_transaction({
            "from": self.address,
            "nonce": self.w3.eth.get_transaction_count(self.address),
            "gas": 100000,
            "gasPrice": self.w3.eth.gas_price,
        })

        # Sign and send
        signed_tx = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)

        # Wait for confirmation
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)

        logger.info(f"USDC approval tx: {tx_hash.hex()}")
        return {"tx_hash": tx_hash.hex(), "status": receipt["status"]}

    def approve_ctf(self, operator: str) -> Dict[str, Any]:
        """
        Approve CTF (Conditional Tokens) for an operator.

        Args:
            operator: Contract address to approve

        Returns:
            Transaction receipt
        """
        logger.info(f"Approving CTF for {operator}...")

        # Build transaction
        tx = self.ctf_contract.functions.setApprovalForAll(
            Web3.to_checksum_address(operator),
            True,
        ).build_transaction({
            "from": self.address,
            "nonce": self.w3.eth.get_transaction_count(self.address),
            "gas": 100000,
            "gasPrice": self.w3.eth.gas_price,
        })

        # Sign and send
        signed_tx = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)

        # Wait for confirmation
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)

        logger.info(f"CTF approval tx: {tx_hash.hex()}")
        return {"tx_hash": tx_hash.hex(), "status": receipt["status"]}

    def approve_all_exchanges(self) -> List[Dict[str, Any]]:
        """
        Approve all exchange contracts for both USDC and CTF.

        This is required before trading on Polymarket.

        Returns:
            List of transaction results
        """
        results = []

        for exchange in self.exchange_addresses:
            exchange_name = self._get_exchange_name(exchange)
            logger.info(f"Setting up approvals for {exchange_name}...")

            # Check and approve USDC if needed
            usdc_allowance = self.check_usdc_allowance(exchange)
            if usdc_allowance < 1000000:  # Less than 1M USDC approved
                result = self.approve_usdc(exchange)
                results.append({
                    "exchange": exchange_name,
                    "token": "USDC",
                    **result,
                })
            else:
                logger.info(f"USDC already approved for {exchange_name}")

            # Check and approve CTF if needed
            ctf_approved = self.check_ctf_approval(exchange)
            if not ctf_approved:
                result = self.approve_ctf(exchange)
                results.append({
                    "exchange": exchange_name,
                    "token": "CTF",
                    **result,
                })
            else:
                logger.info(f"CTF already approved for {exchange_name}")

        return results

    def _get_exchange_name(self, address: str) -> str:
        """Get human-readable name for exchange address."""
        address_lower = address.lower()
        if address_lower == self.config.EXCHANGE_ADDRESS.lower():
            return "Exchange"
        elif address_lower == self.config.NEG_RISK_EXCHANGE_ADDRESS.lower():
            return "NegRiskExchange"
        elif address_lower == self.config.NEG_RISK_ADAPTER_ADDRESS.lower():
            return "NegRiskAdapter"
        return address[:10] + "..."

    def print_status(self) -> None:
        """Print current allowance status."""
        print("\n" + "=" * 60)
        print("ALLOWANCE STATUS")
        print("=" * 60)

        print(f"\nWallet: {self.address}")
        print(f"USDC Balance: ${self.get_usdc_balance():,.2f}")
        print(f"MATIC Balance: {self.get_matic_balance():.4f}")

        print("\n--- Allowances ---")
        allowances = self.check_all_allowances()

        for name, status in allowances.items():
            usdc_ok = "OK" if status["usdc_allowance"] > 1000000 else "NEEDS APPROVAL"
            ctf_ok = "OK" if status["ctf_approved"] else "NEEDS APPROVAL"
            print(f"\n{name}:")
            print(f"  USDC: {usdc_ok} (allowance: ${status['usdc_allowance']:,.0f})")
            print(f"  CTF:  {ctf_ok}")

        print("\n" + "=" * 60)
