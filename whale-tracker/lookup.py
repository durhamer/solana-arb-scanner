"""
Lookup smart wallets holding a specific token.

Usage:
    python main.py lookup <token_mint_address>
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import aiohttp
import requests

from config import DEXSCREENER_TOKEN_URL, HELIUS_API_KEY, KNOWN_DEX_ADDRESSES
from analyzer import _analyze_transactions, _fetch_swap_transactions, _score_wallet
from wallet_discovery import _find_first_buy_price, fetch_top_holders_helius

LOOKUP_RESULTS_DIR = "lookup_results"
VERIFIED_WALLETS_FILE = "verified_wallets.json"
MIN_LOOKUP_SCORE = 85
SLEEP_BETWEEN_REQUESTS = 0.5


def _fetch_token_info(token_mint: str) -> dict | None:
    """Fetch token pair info from DexScreener; return best-liquidity pair or None."""
    url = DEXSCREENER_TOKEN_URL.format(address=token_mint)
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        if not pairs:
            return None
        return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    except Exception as e:
        print(f"[warn] DexScreener fetch failed: {e}")
        return None


def _add_to_verified_wallets(wallets: list[dict], symbol: str, token_mint: str) -> None:
    """Append wallets to verified_wallets.json, skipping duplicates."""
    try:
        with open(VERIFIED_WALLETS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"generated_at": datetime.now(timezone.utc).isoformat(), "wallets": []}

    existing = {w["address"] for w in data.get("wallets", [])}
    added = 0
    for w in wallets:
        if w["address"] not in existing:
            data["wallets"].append({
                "address": w["address"],
                "labels": [f"top holder of {symbol}"],
                "tokens": [symbol],
                "score": w["score"],
                "score_reasons": w["score_reasons"],
                "avg_entry_multiplier": w["avg_entry_multiplier"],
                "appearances_in_tracked_list": 1,
                "swap_tx_count": w["swap_tx_count"],
                "unique_tokens_traded": w["unique_tokens_traded"],
                "tx_per_day": w["tx_per_day"],
                "has_sell_records": w["has_sell_records"],
                "tags": ["smart_money", "top_holder"],
                "source": f"lookup:{symbol}/{token_mint[:8]}",
                "discovered_at": w["discovered_at"],
            })
            added += 1

    data["verified_count"] = len(data["wallets"])
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    with open(VERIFIED_WALLETS_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"[lookup] Added {added} wallet(s) to {VERIFIED_WALLETS_FILE}")


async def _fetch_holders(token_mint: str) -> list[str]:
    async with aiohttp.ClientSession() as session:
        return await fetch_top_holders_helius(session, token_mint)


def run_lookup(token_mint: str) -> None:
    if not HELIUS_API_KEY:
        print("[error] HELIUS_API_KEY not set.")
        return

    # 1. Fetch token info from DexScreener
    pair = _fetch_token_info(token_mint)
    if not pair:
        print(f"[error] Token not found on DexScreener: {token_mint}")
        return

    symbol = (pair.get("baseToken") or {}).get("symbol", "UNKNOWN")
    price_usd = float(pair.get("priceUsd") or 0)
    liquidity_usd = float((pair.get("liquidity") or {}).get("usd") or 0)

    print(f"\n🔍 Token: {symbol} ({token_mint[:8]}...)")
    print(f"💰 Price: ${price_usd:.6f} | Liquidity: ${liquidity_usd:,.0f}")
    print()

    # 2. Fetch top holders via Helius getTokenLargestAccounts
    print("[lookup] Fetching top holders...")
    holders = asyncio.run(_fetch_holders(token_mint))

    if not holders:
        print("[warn] No holders found.")
        return

    print(f"[lookup] Analyzing {len(holders)} holders...\n")

    # 3. Score each holder
    smart_wallets: list[dict] = []

    for i, address in enumerate(holders, 1):
        print(f"  [{i}/{len(holders)}] {address[:8]}...", end="", flush=True)

        txs = _fetch_swap_transactions(address)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

        tx_analysis = _analyze_transactions(txs)

        # Compute entry multiplier for this token specifically
        avg_multiplier = 0.0
        if txs and price_usd > 0:
            buy_price = _find_first_buy_price(txs, address, token_mint)
            if buy_price and buy_price > 0:
                avg_multiplier = price_usd / buy_price

        score, reasons = _score_wallet(
            tx_analysis=tx_analysis,
            address_count_in_list=1,
            avg_entry_multiplier=avg_multiplier,
        )

        print(f" score={score}")

        if score >= MIN_LOOKUP_SCORE:
            smart_wallets.append({
                "address": address,
                "score": score,
                "score_reasons": reasons,
                "avg_entry_multiplier": round(avg_multiplier, 2),
                "swap_tx_count": tx_analysis["tx_count"],
                "unique_tokens_traded": len(tx_analysis["unique_tokens_bought"]),
                "tx_per_day": round(tx_analysis["tx_per_day"], 2),
                "has_sell_records": tx_analysis["has_sell_records"],
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            })

    # 4. Print results
    print()
    if not smart_wallets:
        print("No smart wallets found.")
        return

    print(f"Found {len(smart_wallets)} smart wallets (score >= {MIN_LOOKUP_SCORE}):\n")
    for w in sorted(smart_wallets, key=lambda x: x["score"], reverse=True):
        addr = w["address"]
        short = addr[:6] + "..." + addr[-4:]
        n_tokens = w["unique_tokens_traded"]
        tx_day = w["tx_per_day"]
        has_sells = "has sells" if w["has_sell_records"] else "no sells"
        mult = w["avg_entry_multiplier"]
        entry_str = f"entry {mult:.0f}x" if mult > 0 else "entry unknown"
        print(f"[{w['score']}分] {short}")
        print(f"  traded {n_tokens} tokens | {tx_day} tx/day | {has_sells} | {entry_str}")
        print()

    # 5. Save to lookup_results/
    os.makedirs(LOOKUP_RESULTS_DIR, exist_ok=True)
    filename = f"{symbol}_{token_mint[:8]}.json"
    filepath = os.path.join(LOOKUP_RESULTS_DIR, filename)
    output = {
        "lookup_at": datetime.now(timezone.utc).isoformat(),
        "token_symbol": symbol,
        "token_mint": token_mint,
        "price_usd": price_usd,
        "liquidity_usd": liquidity_usd,
        "smart_wallets": smart_wallets,
    }
    with open(filepath, "w") as f:
        json.dump(output, f, indent=2)
    print(f"[lookup] Saved to {filepath}")

    # 6. Offer to add to verified_wallets.json
    try:
        answer = input(
            f"\nAdd {len(smart_wallets)} wallet(s) to verified_wallets.json for tracking? [y/N] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""

    if answer == "y":
        _add_to_verified_wallets(smart_wallets, symbol, token_mint)
    else:
        print(f"[lookup] Skipped. Wallets saved in {filepath}")
