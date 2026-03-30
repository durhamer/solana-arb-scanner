"""
Discover "smart money" wallets from public sources.

Flow:
1. Fetch top boosted tokens from DexScreener
2. For each token, fetch pair data (price, volume, liquidity)
3. Attempt to find top holders via Birdeye or Solscan (fallback)
4. Filter out DEX pools / program addresses
5. Score remaining wallets by early-entry + hold-through-5x behaviour
6. Save results to tracked_wallets.json
"""

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from config import (
    ALL_MANUAL_WALLETS,
    BIRDEYE_API_KEY,
    BIRDEYE_API_URL,
    DEXSCREENER_TOKEN_URL,
    DEXSCREENER_TOP_BOOSTS_URL,
    HELIUS_API_KEY,
    HELIUS_RPC_URL,
    KNOWN_DEX_ADDRESSES,
    MIN_PRICE_MULTIPLIER,
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
    SOLANA_RPC_URL,
    SOLSCAN_API_KEY,
    SOLSCAN_API_URL,
    TOP_HOLDERS_LIMIT,
    TOP_TOKENS_LIMIT,
    TRACKED_WALLETS_FILE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _sleep() -> None:
    """Polite delay between API requests to avoid rate limiting."""
    delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
    await asyncio.sleep(delay)


def _is_personal_wallet(address: str) -> bool:
    """Return True if address looks like a personal wallet (not a program/pool)."""
    if address in KNOWN_DEX_ADDRESSES:
        return False
    # Solana program addresses are typically 32-byte base58; simple heuristic:
    # known programs tend to have trailing repeated chars or specific prefixes.
    # A more robust check requires on-chain account type lookup (owner == system program).
    # For now we rely on the exclusion list.
    return True


# ---------------------------------------------------------------------------
# DexScreener
# ---------------------------------------------------------------------------

async def fetch_top_boosted_tokens(session: aiohttp.ClientSession) -> list[dict]:
    """Return top boosted token entries from DexScreener."""
    log.info("Fetching top boosted tokens from DexScreener...")
    try:
        async with session.get(DEXSCREENER_TOP_BOOSTS_URL) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except Exception as exc:
        log.error("Failed to fetch top boosts: %s", exc)
        return []

    # Response is a list of boost objects; keep Solana tokens only
    tokens = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("chainId") == "solana":
                tokens.append(item)
    elif isinstance(data, dict):
        # Some versions wrap in {"tokenBoosts": [...]}
        for item in data.get("tokenBoosts", []):
            if isinstance(item, dict) and item.get("chainId") == "solana":
                tokens.append(item)

    log.info("Found %d Solana boosted tokens", len(tokens))
    return tokens[:TOP_TOKENS_LIMIT]


async def fetch_token_pairs(
    session: aiohttp.ClientSession, token_address: str
) -> list[dict]:
    """Return DexScreener pair data for a token address."""
    url = DEXSCREENER_TOKEN_URL.format(address=token_address)
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("pairs") or []
    except Exception as exc:
        log.warning("Failed to fetch pairs for %s: %s", token_address, exc)
        return []


def _best_pair(pairs: list[dict]) -> dict | None:
    """Pick the pair with the highest liquidity."""
    if not pairs:
        return None
    return max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))


# ---------------------------------------------------------------------------
# Birdeye (free tier, no key required for some endpoints)
# ---------------------------------------------------------------------------

async def fetch_top_holders_birdeye(
    session: aiohttp.ClientSession, token_address: str
) -> list[dict]:
    """
    Fetch top token holders from Birdeye public API.

    Endpoint: GET /defi/token_security  or  GET /v1/token/holder
    NOTE: As of 2024 Birdeye requires an API key for most endpoints.
    TODO: Replace with a valid BIRDEYE_API_KEY to enable this path.
    """
    if not BIRDEYE_API_KEY:
        log.debug("No Birdeye API key — skipping Birdeye top-holder lookup")
        return []

    url = f"{BIRDEYE_API_URL}/v1/token/holder"
    headers = {"X-API-KEY": BIRDEYE_API_KEY}
    params = {"address": token_address, "offset": 0, "limit": TOP_HOLDERS_LIMIT}
    try:
        async with session.get(url, headers=headers, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
            items = data.get("data", {}).get("items", [])
            log.info(
                "Birdeye: %d holders for %s", len(items), token_address[:8] + "..."
            )
            return items
    except Exception as exc:
        log.warning("Birdeye holder fetch failed for %s: %s", token_address, exc)
        return []


# ---------------------------------------------------------------------------
# Solscan (public API, limited without key)
# ---------------------------------------------------------------------------

async def fetch_top_holders_solscan(
    session: aiohttp.ClientSession, token_address: str
) -> list[dict]:
    """
    Fetch top token holders from Solscan public API.

    Endpoint: GET /token/holders
    TODO: Add SOLSCAN_API_KEY for higher rate limits.
    """
    url = f"{SOLSCAN_API_URL}/token/holders"
    headers = {}
    if SOLSCAN_API_KEY:
        headers["token"] = SOLSCAN_API_KEY
    params = {"tokenAddress": token_address, "limit": TOP_HOLDERS_LIMIT, "offset": 0}
    try:
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 429:
                log.warning("Solscan rate limited for %s", token_address)
                return []
            resp.raise_for_status()
            data = await resp.json()
            items = data.get("data", [])
            log.info(
                "Solscan: %d holders for %s", len(items), token_address[:8] + "..."
            )
            return items
    except Exception as exc:
        log.warning("Solscan holder fetch failed for %s: %s", token_address, exc)
        return []


# ---------------------------------------------------------------------------
# Solana RPC helpers
# ---------------------------------------------------------------------------

async def rpc_post(
    session: aiohttp.ClientSession, method: str, params: list
) -> Any:
    """Generic Solana JSON-RPC POST."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with session.post(SOLANA_RPC_URL, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("result")
    except Exception as exc:
        log.warning("RPC %s failed: %s", method, exc)
        return None


async def is_system_account(session: aiohttp.ClientSession, address: str) -> bool:
    """
    Return True if address is owned by the System Program (i.e. a regular wallet).
    Token program accounts / program accounts are excluded.
    """
    result = await rpc_post(
        session,
        "getAccountInfo",
        [address, {"encoding": "base58"}],
    )
    if result is None:
        return False
    owner = (result.get("value") or {}).get("owner", "")
    # System program: 11111111111111111111111111111111
    return owner == "11111111111111111111111111111111"


async def fetch_recent_transactions(
    session: aiohttp.ClientSession, address: str, limit: int = 10
) -> list[dict]:
    """
    Fetch recent transaction signatures for a wallet via Solana RPC.
    Full historical analysis requires Helius or Solscan.
    TODO: Use HELIUS_API_KEY for deep history scan.
    """
    result = await rpc_post(
        session,
        "getSignaturesForAddress",
        [address, {"limit": limit}],
    )
    return result or []


# ---------------------------------------------------------------------------
# Helius (free tier, needs API key)
# ---------------------------------------------------------------------------

HELIUS_SLEEP = 0.5  # seconds between Helius requests (free tier is lenient)

# Stablecoin mints used to estimate USD cost of a swap
STABLECOINS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


async def _helius_sleep() -> None:
    await asyncio.sleep(HELIUS_SLEEP)


def _helius_rpc_url() -> str:
    return HELIUS_RPC_URL.format(api_key=HELIUS_API_KEY)


async def _helius_rpc_post(
    session: aiohttp.ClientSession, method: str, params: list
) -> Any:
    """JSON-RPC POST to Helius RPC endpoint."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with session.post(_helius_rpc_url(), json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("result")
    except Exception as exc:
        log.warning("Helius RPC %s failed: %s", method, exc)
        return None


async def fetch_top_holders_helius(
    session: aiohttp.ClientSession, token_mint: str
) -> list[str]:
    """
    Return owner wallet addresses for the top token accounts of *token_mint*.

    Steps:
      1. getTokenLargestAccounts  →  list of token accounts (up to 20)
      2. getAccountInfo for each  →  resolve the owner (personal wallet)
    """
    if not HELIUS_API_KEY:
        log.debug("No Helius API key — skipping Helius holder lookup")
        return []

    log.info("Helius: fetching largest token accounts for %s...", token_mint[:8] + "...")
    result = await _helius_rpc_post(
        session, "getTokenLargestAccounts", [token_mint]
    )
    await _helius_sleep()

    token_accounts = (result or {}).get("value", [])
    if not token_accounts:
        log.info("Helius: no token accounts returned for %s", token_mint[:8] + "...")
        return []

    owner_addresses: list[str] = []
    for ta in token_accounts[:TOP_HOLDERS_LIMIT]:
        ta_address = ta.get("address")
        if not ta_address:
            continue

        account_info = await _helius_rpc_post(
            session,
            "getAccountInfo",
            [ta_address, {"encoding": "jsonParsed"}],
        )
        await _helius_sleep()

        owner = (
            ((account_info or {}).get("value") or {})
            .get("data", {})
            .get("parsed", {})
            .get("info", {})
            .get("owner")
        )
        if owner and owner not in KNOWN_DEX_ADDRESSES:
            owner_addresses.append(owner)

    log.info(
        "Helius: resolved %d owner wallets for %s",
        len(owner_addresses),
        token_mint[:8] + "...",
    )
    return owner_addresses


async def fetch_parsed_transactions_helius(
    session: aiohttp.ClientSession,
    address: str,
    limit: int = 100,
    tx_type: str = "",
) -> list[dict]:
    """
    Use Helius Enhanced Transactions API to get parsed tx history.
    Pass tx_type="SWAP" to filter for swap transactions only.
    """
    if not HELIUS_API_KEY:
        log.debug("No Helius API key — skipping parsed tx lookup")
        return []

    url = (
        f"https://api.helius.xyz/v0/addresses/{address}/transactions"
        f"?api-key={HELIUS_API_KEY}&limit={limit}"
    )
    if tx_type:
        url += f"&type={tx_type}"
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()
    except Exception as exc:
        log.warning("Helius tx fetch failed for %s: %s", address, exc)
        return []


def _find_first_buy_price(
    txs: list[dict], wallet_address: str, token_mint: str
) -> float | None:
    """
    Scan *txs* (oldest-last order from Helius) for the earliest SWAP where
    *wallet_address* received *token_mint* and calculate price in USD.

    Returns price-per-token in USD, or None if undeterminable.
    """
    # Helius returns newest-first; reverse to find the earliest buy
    for tx in reversed(txs):
        if tx.get("type") != "SWAP":
            continue

        token_in: float = 0.0
        usd_out: float = 0.0

        for transfer in tx.get("tokenTransfers", []):
            if (
                transfer.get("mint") == token_mint
                and transfer.get("toUserAccount") == wallet_address
            ):
                token_in += float(transfer.get("tokenAmount") or 0)

        if token_in <= 0:
            continue  # wallet didn't receive this token in this tx

        # Try to estimate USD spent via nativeTransfers (SOL) or stablecoin transfers
        for transfer in tx.get("nativeTransfers", []):
            if transfer.get("fromUserAccount") == wallet_address:
                lamports = float(transfer.get("amount") or 0)
                # Rough SOL price: use a fallback of $150 if we can't look it up
                # A real implementation would fetch SOL price at tx timestamp
                sol_price_usd = 150.0
                usd_out += (lamports / 1e9) * sol_price_usd

        # Also check for stablecoin outflows (USDC/USDT)
        for transfer in tx.get("tokenTransfers", []):
            if (
                transfer.get("mint") in STABLECOINS
                and transfer.get("fromUserAccount") == wallet_address
            ):
                usd_out += float(transfer.get("tokenAmount") or 0)

        if token_in > 0 and usd_out > 0:
            return usd_out / token_in

    return None


async def score_wallet_helius(
    session: aiohttp.ClientSession,
    wallet_address: str,
    token_mint: str,
    current_price_usd: float,
) -> float:
    """
    Fetch SWAP history for *wallet_address* via Helius, find earliest buy of
    *token_mint*, and return current_price / buy_price (the multiplier).
    Returns 0.0 if we can't determine the buy price.
    """
    txs = await fetch_parsed_transactions_helius(
        session, wallet_address, limit=100, tx_type="SWAP"
    )
    await _helius_sleep()

    if not txs:
        return 0.0

    buy_price = _find_first_buy_price(txs, wallet_address, token_mint)
    if buy_price and buy_price > 0 and current_price_usd > 0:
        return current_price_usd / buy_price
    return 0.0


# ---------------------------------------------------------------------------
# Wallet scoring / filtering
# ---------------------------------------------------------------------------

def _extract_holder_address(holder_item: dict) -> str | None:
    """Normalise holder dicts from Birdeye / Solscan API responses."""
    if "owner" in holder_item:
        return holder_item["owner"]
    if "address" in holder_item:
        return holder_item["address"]
    return None


def _build_wallet_entry(
    address: str,
    label: str,
    source: str,
    tags: list[str],
    extra: dict | None = None,
) -> dict:
    entry = {
        "address": address,
        "label": label,
        "source": source,
        "discovered_at": _now_iso(),
        "tags": tags,
    }
    if extra:
        entry.update(extra)
    return entry


async def score_wallet_for_early_entry(
    session: aiohttp.ClientSession,
    address: str,
    token_address: str,
    token_symbol: str,
    current_price_usd: float,
) -> float:
    """
    Return estimated price multiplier (current_price / buy_price) for *address*.
    Priority: Helius (full SWAP history) → public RPC fallback (nominal score).
    """
    if HELIUS_API_KEY:
        return await score_wallet_helius(session, address, token_address, current_price_usd)

    # Fallback: public RPC — can only confirm wallet is active, not buy price
    txs = await fetch_recent_transactions(session, address, limit=5)
    if txs:
        return 1.0
    return 0.0


# ---------------------------------------------------------------------------
# Main discovery orchestration
# ---------------------------------------------------------------------------

async def discover_smart_money() -> list[dict]:
    """
    Full discovery pipeline. Returns a list of wallet entry dicts.
    """
    discovered: list[dict] = []
    seen_addresses: set[str] = set()

    # --- Seed with manually curated wallets ---
    for w in ALL_MANUAL_WALLETS:
        addr = w.get("address", "")
        if addr and addr not in seen_addresses:
            seen_addresses.add(addr)
            discovered.append(
                _build_wallet_entry(
                    address=addr,
                    label=w.get("label", "manual"),
                    source="manual",
                    tags=w.get("tags", ["manual"]),
                )
            )

    connector = aiohttp.TCPConnector(limit=5)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # -----------------------------------------------------------------
        # Step 1: Top boosted tokens on DexScreener
        # -----------------------------------------------------------------
        boosted_tokens = await fetch_top_boosted_tokens(session)
        await _sleep()

        for token_obj in boosted_tokens:
            token_address = token_obj.get("tokenAddress") or token_obj.get("address")
            if not token_address:
                continue

            log.info("Analysing token: %s", token_address[:8] + "...")

            # -----------------------------------------------------------------
            # Step 2: Get pair data to understand price & liquidity context
            # -----------------------------------------------------------------
            pairs = await fetch_token_pairs(session, token_address)
            await _sleep()

            best = _best_pair(pairs)
            if not best:
                log.info("  No pairs found, skipping")
                continue

            token_symbol = best.get("baseToken", {}).get("symbol", "UNKNOWN")
            current_price = float(best.get("priceUsd") or 0)
            price_change_h24 = float(
                (best.get("priceChange") or {}).get("h24") or 0
            )
            liquidity_usd = float(
                (best.get("liquidity") or {}).get("usd") or 0
            )

            log.info(
                "  %s | price=$%.6f | 24h_chg=%.1f%% | liq=$%.0f",
                token_symbol,
                current_price,
                price_change_h24,
                liquidity_usd,
            )

            # Skip tokens with very low liquidity (likely rugs)
            if liquidity_usd < 10_000:
                log.info("  Liquidity too low, skipping")
                continue

            # -----------------------------------------------------------------
            # Step 3: Fetch top holders  Helius → Birdeye → Solscan
            # -----------------------------------------------------------------
            holder_source = "unknown"
            owner_addrs: list[str] = []

            if HELIUS_API_KEY:
                owner_addrs = await fetch_top_holders_helius(session, token_address)
                if owner_addrs:
                    holder_source = "helius"

            if not owner_addrs:
                raw_holders = await fetch_top_holders_birdeye(session, token_address)
                await _sleep()
                if raw_holders:
                    holder_source = "birdeye"
                    owner_addrs = [
                        a for h in raw_holders
                        if (a := _extract_holder_address(h))
                    ]

            if not owner_addrs:
                raw_holders = await fetch_top_holders_solscan(session, token_address)
                await _sleep()
                if raw_holders:
                    holder_source = "solscan"
                    owner_addrs = [
                        a for h in raw_holders
                        if (a := _extract_holder_address(h))
                    ]

            if not owner_addrs:
                log.info(
                    "  Could not fetch holders for %s — "
                    "add HELIUS_API_KEY, BIRDEYE_API_KEY, or SOLSCAN_API_KEY in config.py",
                    token_symbol,
                )
                continue

            log.info("  %d holders via %s", len(owner_addrs), holder_source)

            # -----------------------------------------------------------------
            # Step 4: Filter and score holders
            # -----------------------------------------------------------------
            for addr in owner_addrs:
                if not addr:
                    continue
                if addr in seen_addresses:
                    continue
                if not _is_personal_wallet(addr):
                    log.debug("  Skipping known DEX/program address: %s", addr)
                    continue

                # Score the wallet
                score = await score_wallet_for_early_entry(
                    session, addr, token_address, token_symbol, current_price
                )
                if not HELIUS_API_KEY:
                    await _sleep()

                # When Helius scoring is active, only keep 5x+ early buyers.
                # Without Helius we fall back to nominal score of 1.0 and keep all.
                if HELIUS_API_KEY and score < MIN_PRICE_MULTIPLIER:
                    log.debug(
                        "  Skipping %s... (multiplier=%.1fx < %.0fx threshold)",
                        addr[:8], score, MIN_PRICE_MULTIPLIER,
                    )
                    continue

                tags = ["smart_money", "top_holder"]
                if score >= MIN_PRICE_MULTIPLIER:
                    tags.append("early_buyer_5x")

                seen_addresses.add(addr)
                entry = _build_wallet_entry(
                    address=addr,
                    label=f"top holder of {token_symbol}",
                    source=f"dexscreener+{holder_source} discovery",
                    tags=tags,
                    extra={
                        "token": token_symbol,
                        "token_address": token_address,
                        "estimated_entry_multiplier": score,
                        "token_price_usd": current_price,
                        "token_liquidity_usd": liquidity_usd,
                    },
                )
                discovered.append(entry)
                log.info("  + Added wallet %s... (%s)", addr[:8], ", ".join(tags))

    log.info("Discovery complete. Total wallets: %d", len(discovered))
    return discovered


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_existing_wallets(path: str = TRACKED_WALLETS_FILE) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            log.warning("Could not parse %s — starting fresh", path)
    return {"wallets": []}


def save_wallets(wallets: list[dict], path: str = TRACKED_WALLETS_FILE) -> None:
    existing = load_existing_wallets(path)
    existing_addrs = {w["address"] for w in existing["wallets"]}

    new_count = 0
    for w in wallets:
        if w["address"] not in existing_addrs:
            existing["wallets"].append(w)
            existing_addrs.add(w["address"])
            new_count += 1

    Path(path).write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    log.info("Saved %d new wallets to %s (total: %d)", new_count, path, len(existing["wallets"]))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_discovery(output_file: str = TRACKED_WALLETS_FILE) -> list[dict]:
    wallets = await discover_smart_money()
    save_wallets(wallets, output_file)
    return wallets


if __name__ == "__main__":
    asyncio.run(run_discovery())
