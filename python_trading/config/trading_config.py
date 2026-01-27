"""
Trading configuration for Polymarket CLOB API.
Uses L1 authentication (private key + funder) without L2 API keys.

SECURITY WARNING:
- Never commit your private key to git
- Use environment variables or a .env file
- Add .env to .gitignore
"""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class TradingConfig:
    """Configuration for Polymarket trading."""

    # === CLOB API ===
    CLOB_HOST: str = "https://clob.polymarket.com"
    CHAIN_ID: int = 137  # Polygon Mainnet

    # === Authentication L1 ===
    # signature_type:
    #   0 = EOA (MetaMask, hardware wallet, direct private key)
    #   1 = Email/Magic wallet (delegated signing)
    #   2 = Browser wallet proxy
    SIGNATURE_TYPE: int = 0  # EOA for pk + funder

    # === Token Contracts (Polygon) ===
    USDC_ADDRESS: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    CTF_ADDRESS: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

    # === Exchange Contracts (need allowance) ===
    EXCHANGE_ADDRESS: str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8a8EF69"
    NEG_RISK_EXCHANGE_ADDRESS: str = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
    NEG_RISK_ADAPTER_ADDRESS: str = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

    # === Order Defaults ===
    DEFAULT_ORDER_TYPE: str = "GTC"  # Good Till Canceled
    MIN_ORDER_SIZE: float = 5.0  # Minimum shares
    MAX_ORDER_SIZE: float = 10000.0  # Maximum shares per order

    # === Rate Limiting ===
    MAX_REQUESTS_PER_SECOND: int = 10
    REQUEST_TIMEOUT: int = 30

    @classmethod
    def from_env(cls) -> "TradingConfig":
        """Load config with environment variable overrides."""
        config = cls()

        if os.getenv("CLOB_HOST"):
            config.CLOB_HOST = os.getenv("CLOB_HOST")

        if os.getenv("CHAIN_ID"):
            config.CHAIN_ID = int(os.getenv("CHAIN_ID"))

        if os.getenv("SIGNATURE_TYPE"):
            config.SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE"))

        return config


def get_private_key() -> str:
    """
    Get private key from environment variable.

    Returns:
        Private key string (with or without 0x prefix)

    Raises:
        ValueError: If PRIVATE_KEY env var is not set
    """
    pk = os.getenv("PRIVATE_KEY")
    if not pk:
        raise ValueError(
            "PRIVATE_KEY environment variable not set. "
            "Set it with: export PRIVATE_KEY='your-private-key'"
        )
    return pk


def get_funder_address() -> Optional[str]:
    """
    Get funder address from environment variable.

    The funder is the address that holds your funds on Polymarket.
    For EOA wallets, this is usually the same as your wallet address.
    For proxy wallets, this is the address that actually holds the funds.

    Returns:
        Funder address or None if not set
    """
    return os.getenv("FUNDER_ADDRESS")


def get_rpc_url() -> str:
    """
    Get Polygon RPC URL for on-chain operations.

    Returns:
        RPC URL string
    """
    return os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")


# === Environment Template ===
ENV_TEMPLATE = """
# Polymarket Trading Configuration
# Copy this to .env and fill in your values

# Your wallet's private key (required)
# WARNING: Never share or commit this!
PRIVATE_KEY=

# Funder address (optional, defaults to wallet address derived from pk)
# Use this if your funds are in a different address than your signing key
FUNDER_ADDRESS=

# Polygon RPC URL (optional, has default)
POLYGON_RPC_URL=https://polygon-rpc.com

# CLOB API host (optional, has default)
CLOB_HOST=https://clob.polymarket.com

# Chain ID (optional, defaults to 137 for Polygon)
CHAIN_ID=137

# Signature type (optional, defaults to 0 for EOA)
# 0 = EOA (MetaMask, hardware wallet)
# 1 = Email/Magic wallet
# 2 = Browser wallet proxy
SIGNATURE_TYPE=0
"""


def create_env_template(path: str = ".env.template"):
    """Create a template .env file."""
    with open(path, "w") as f:
        f.write(ENV_TEMPLATE.strip())
    print(f"Created {path} - copy to .env and fill in your values")
