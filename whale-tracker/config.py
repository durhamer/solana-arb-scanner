"""
Constants: RPC endpoints, tracked wallet addresses, token list
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Solana RPC endpoints (public, no key required)
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
SOLANA_RPC_BACKUP = "https://solana-api.projectserum.com"

# DexScreener API (public, no key required)
DEXSCREENER_TOP_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"

# Birdeye public API (free tier, no key required for basic endpoints)
# TODO: Add BIRDEYE_API_KEY for higher rate limits and more endpoints
BIRDEYE_API_URL = "https://public-api.birdeye.so"
BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY", "")

# Solscan public API (no key required for basic endpoints)
# TODO: Add SOLSCAN_API_KEY for higher rate limits
SOLSCAN_API_URL = "https://public-api.solscan.io"
SOLSCAN_API_KEY = os.environ.get("SOLSCAN_API_KEY", "")

# Helius API (free tier available)
# TODO: Add HELIUS_API_KEY for transaction history queries
HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/?api-key={api_key}"
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")

# Email notification (Gmail SMTP)
# Set EMAIL_SENDER to a Gmail address and EMAIL_PASSWORD to an App Password
# (https://myaccount.google.com/apppasswords — requires 2FA to be enabled).
# Leave EMAIL_SENDER empty to disable email alerts entirely.
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER", "")

# File paths
TRACKED_WALLETS_FILE = "tracked_wallets.json"

# Discovery settings
TOP_TOKENS_LIMIT = 10          # How many top boosted tokens to analyze
TOP_HOLDERS_LIMIT = 20         # How many top holders to check per token
MIN_PRICE_MULTIPLIER = 5.0     # Minimum price increase to qualify as "smart money"
REQUEST_DELAY_MIN = 1.0        # Min seconds between API requests
REQUEST_DELAY_MAX = 2.0        # Max seconds between API requests

# Known DEX pool / program addresses to exclude (not personal wallets)
KNOWN_DEX_ADDRESSES = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",  # Orca Whirlpool
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpool program
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",   # Jupiter v4
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token program
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bau",  # Associated Token Account
    "So11111111111111111111111111111111111111112",    # Wrapped SOL
    "11111111111111111111111111111111",               # System program
    "SysvarRent111111111111111111111111111111111",    # Sysvar rent
    "SysvarC1ock11111111111111111111111111111111",    # Sysvar clock
}

# ---------------------------------------------------------------
# Manually curated wallet lists (fill these in manually)
# ---------------------------------------------------------------

# Known VC / fund addresses
# TODO: Fill in known addresses for a16z, Multicoin Capital, Polychain Capital, etc.
# These are publicly known Solana wallets associated with major funds.
# Sources: on-chain analytics, public announcements, Solscan labels
VC_WALLETS: list[dict] = [
    # Example format:
    # {"address": "xxx", "label": "a16z", "tags": ["vc", "institutional"]},
    # {"address": "yyy", "label": "Multicoin Capital", "tags": ["vc", "institutional"]},
]

# Known skilled trader addresses
# TODO: Fill in addresses of well-known on-chain traders / alpha callers
# Sources: Twitter/X alpha accounts that share on-chain activity, Nansen labels
KNOWN_TRADER_WALLETS: list[dict] = [
    # Example format:
    # {"address": "xxx", "label": "famous_trader_1", "tags": ["smart_money", "trader"]},
]

# Project treasury / team addresses
# TODO: Fill in treasury addresses of projects you want to monitor
# Sources: project documentation, on-chain multisig explorers
TREASURY_WALLETS: list[dict] = [
    # Example format:
    # {"address": "xxx", "label": "ProjectX Treasury", "tags": ["treasury", "team"]},
]

# Combine all manual wallets for easy access
ALL_MANUAL_WALLETS: list[dict] = VC_WALLETS + KNOWN_TRADER_WALLETS + TREASURY_WALLETS
