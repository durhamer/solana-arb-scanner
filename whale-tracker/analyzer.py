"""
Analyze transaction patterns and score wallets as "smart money".

Usage:
    python main.py analyze
"""

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import requests

from config import HELIUS_API_KEY, TRACKED_WALLETS_FILE

VERIFIED_WALLETS_FILE = "verified_wallets.json"
HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
HELIUS_PARSE_TX_URL = "https://api.helius.xyz/v0/transactions"
MIN_SCORE = 60
SLEEP_BETWEEN_REQUESTS = 0.5


def _load_tracked_wallets() -> list[dict]:
    with open(TRACKED_WALLETS_FILE) as f:
        data = json.load(f)
    return data.get("wallets", [])


def _fetch_swap_transactions(address: str) -> list[dict]:
    """Fetch last 50 SWAP transactions for a wallet via Helius.

    Strategy:
    1. Try GET /v0/addresses/{address}/transactions?type=SWAP (enhanced tx API).
    2. On 404, fall back to getSignaturesForAddress via Helius RPC, then batch-parse
       the signatures with POST /v0/transactions.
    3. If both fail, return [] so the caller can assign score=0.
    """
    from config import HELIUS_RPC_URL

    url = HELIUS_TX_URL.format(address=address)
    params = {"api-key": HELIUS_API_KEY, "type": "SWAP", "limit": 50}
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 404:
            print(f"  [warn] Enhanced TX API 404 for {address[:8]}..., trying RPC fallback")
        else:
            resp.raise_for_status()
            return resp.json()
    except requests.HTTPError as e:
        print(f"  [warn] Failed to fetch transactions for {address[:8]}...: HTTP {e.response.status_code}")
        return []
    except Exception as e:
        print(f"  [warn] Failed to fetch transactions for {address[:8]}...: {type(e).__name__}")
        return []

    # --- Fallback: getSignaturesForAddress via Helius RPC ---
    rpc_url = HELIUS_RPC_URL.format(api_key=HELIUS_API_KEY)
    try:
        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": 50}],
        }
        rpc_resp = requests.post(rpc_url, json=rpc_payload, timeout=15)
        rpc_resp.raise_for_status()
        sig_result = rpc_resp.json().get("result") or []
        signatures = [item["signature"] for item in sig_result if item.get("signature")]
        if not signatures:
            print(f"  [warn] No signatures found for {address[:8]}... (score=0)")
            return []
    except requests.HTTPError as e:
        print(f"  [warn] RPC getSignaturesForAddress HTTP {e.response.status_code} for {address[:8]}... (score=0)")
        return []
    except Exception as e:
        # Avoid printing the exception directly — it may contain the RPC URL with the API key
        print(f"  [warn] RPC getSignaturesForAddress failed for {address[:8]}...: {type(e).__name__} (score=0)")
        return []

    # --- Batch-parse signatures via POST /v0/transactions ---
    try:
        parse_resp = requests.post(
            HELIUS_PARSE_TX_URL,
            params={"api-key": HELIUS_API_KEY},
            json={"transactions": signatures},
            timeout=20,
        )
        parse_resp.raise_for_status()
        all_txs = parse_resp.json()
        # Keep only SWAP transactions to match the original filter
        return [tx for tx in all_txs if tx.get("type") == "SWAP"]
    except requests.HTTPError as e:
        print(f"  [warn] Batch parse transactions HTTP {e.response.status_code} for {address[:8]}... (score=0)")
        return []
    except Exception as e:
        print(f"  [warn] Batch parse transactions failed for {address[:8]}...: {type(e).__name__} (score=0)")
        return []


def _analyze_transactions(txs: list[dict]) -> dict[str, Any]:
    """
    Extract trading patterns from a list of Helius enhanced swap transactions.

    Returns:
        unique_tokens_bought  – set of token mints the wallet received
        has_sell_records      – True if the wallet sent any non-SOL token (sell side)
        tx_per_day            – average swaps per calendar day over the observed window
        tx_count              – raw transaction count
    """
    if not txs:
        return {
            "unique_tokens_bought": set(),
            "has_sell_records": False,
            "tx_per_day": 0.0,
            "tx_count": 0,
        }

    tokens_bought: set[str] = set()
    tokens_sold: set[str] = set()
    timestamps: list[int] = []

    WSOL = "So11111111111111111111111111111111111111112"

    for tx in txs:
        ts = tx.get("timestamp")
        if ts:
            timestamps.append(ts)

        # Helius enhanced transactions expose a structured swap event
        swap = tx.get("events", {}).get("swap", {})
        if swap:
            for t in swap.get("tokenOutputs", []):
                mint = t.get("mint")
                if mint and mint != WSOL:
                    tokens_bought.add(mint)
            for t in swap.get("tokenInputs", []):
                mint = t.get("mint")
                if mint and mint != WSOL:
                    tokens_sold.add(mint)
            continue

        # Fallback: parse tokenTransfers directly
        for transfer in tx.get("tokenTransfers", []):
            mint = transfer.get("mint")
            if not mint or mint == WSOL:
                continue
            # Determine direction from amount sign or fromUser/toUser
            # Helius sets toUserAccount for the receiving wallet
            to_user = transfer.get("toUserAccount", "")
            from_user = transfer.get("fromUserAccount", "")
            # We don't have the wallet address here; treat any non-SOL received as buy
            # and any sent as sell — approximate but sufficient for scoring
            if to_user:
                tokens_bought.add(mint)
            if from_user:
                tokens_sold.add(mint)

    # tx_per_day over the observed window
    tx_count = len(txs)
    tx_per_day = 0.0
    if len(timestamps) >= 2:
        span_seconds = max(timestamps) - min(timestamps)
        if span_seconds > 0:
            tx_per_day = tx_count / (span_seconds / 86400)

    return {
        "unique_tokens_bought": tokens_bought,
        "has_sell_records": bool(tokens_sold),
        "tx_per_day": tx_per_day,
        "tx_count": tx_count,
    }


def _score_wallet(
    tx_analysis: dict[str, Any],
    address_count_in_list: int,
    avg_entry_multiplier: float,
) -> tuple[int, list[str]]:
    """
    Compute 0-100 smart money score.

    Breakdown:
        +30  多代幣命中 (traded ≥3 different tokens)
        +20  高 entry multiplier 均值 (avg ≥100x)
        +20  交易頻率適中 (0.3–30 swaps/day)
        +15  有賣出紀錄
        +15  在追蹤清單中出現超過一次
    """
    score = 0
    reasons: list[str] = []

    # 1. 多代幣命中 +30
    unique_tokens = tx_analysis["unique_tokens_bought"]
    if len(unique_tokens) >= 3:
        score += 30
        reasons.append(f"traded {len(unique_tokens)} different tokens (+30)")
    elif len(unique_tokens) >= 2:
        score += 15
        reasons.append(f"traded {len(unique_tokens)} different tokens (+15)")

    # 2. 高 entry multiplier 均值 +20
    if avg_entry_multiplier >= 100:
        score += 20
        reasons.append(f"avg entry multiplier {avg_entry_multiplier:.1f}x (+20)")
    elif avg_entry_multiplier >= 20:
        score += 10
        reasons.append(f"avg entry multiplier {avg_entry_multiplier:.1f}x (+10)")

    # 3. 交易頻率適中 +20 (not a bot, not a dead wallet)
    tx_per_day = tx_analysis["tx_per_day"]
    tx_count = tx_analysis["tx_count"]
    if tx_count > 0 and 0.3 <= tx_per_day <= 30:
        score += 20
        reasons.append(f"healthy tx frequency {tx_per_day:.1f}/day (+20)")
    elif tx_count > 0 and tx_per_day > 0:
        score += 5
        reasons.append(f"tx frequency {tx_per_day:.1f}/day (+5)")

    # 4. 有賣出紀錄 +15
    if tx_analysis["has_sell_records"]:
        score += 15
        reasons.append("has sell records (+15)")

    # 5. 在清單中出現超過一次 +15
    if address_count_in_list > 1:
        score += 15
        reasons.append(f"appears {address_count_in_list}x in tracked list (+15)")

    return score, reasons


def run_analysis() -> None:
    if not HELIUS_API_KEY:
        print("[error] HELIUS_API_KEY environment variable not set.")
        return

    wallets = _load_tracked_wallets()
    if not wallets:
        print("[warn] No wallets in tracked_wallets.json")
        return

    print(f"[analyze] Loaded {len(wallets)} entries from {TRACKED_WALLETS_FILE}")

    # Group entries by address
    address_entries: dict[str, list[dict]] = defaultdict(list)
    for w in wallets:
        address_entries[w["address"]].append(w)

    unique_addresses = list(address_entries.keys())
    print(f"[analyze] {len(unique_addresses)} unique addresses to analyze\n")

    results: list[dict] = []

    for i, address in enumerate(unique_addresses, 1):
        entries = address_entries[address]
        count_in_list = len(entries)
        multipliers = [e.get("estimated_entry_multiplier", 0) for e in entries]
        avg_multiplier = sum(multipliers) / len(multipliers) if multipliers else 0.0

        print(
            f"[{i}/{len(unique_addresses)}] {address[:12]}..."
            f"  appearances={count_in_list}"
            f"  avg_mult={avg_multiplier:.0f}x"
        )

        txs = _fetch_swap_transactions(address)
        tx_analysis = _analyze_transactions(txs)

        print(
            f"  swaps={tx_analysis['tx_count']}"
            f"  unique_tokens={len(tx_analysis['unique_tokens_bought'])}"
            f"  tx/day={tx_analysis['tx_per_day']:.2f}"
            f"  has_sells={tx_analysis['has_sell_records']}"
        )

        score, reasons = _score_wallet(
            tx_analysis=tx_analysis,
            address_count_in_list=count_in_list,
            avg_entry_multiplier=avg_multiplier,
        )
        print(f"  score={score}  reasons={reasons}\n")

        results.append(
            {
                "address": address,
                "labels": list({e.get("label", "") for e in entries}),
                "tokens": list({e.get("token", "") for e in entries}),
                "score": score,
                "score_reasons": reasons,
                "avg_entry_multiplier": round(avg_multiplier, 2),
                "appearances_in_tracked_list": count_in_list,
                "swap_tx_count": tx_analysis["tx_count"],
                "unique_tokens_traded": len(tx_analysis["unique_tokens_bought"]),
                "tx_per_day": round(tx_analysis["tx_per_day"], 2),
                "has_sell_records": tx_analysis["has_sell_records"],
                "tags": list({tag for e in entries for tag in e.get("tags", [])}),
                "source": entries[0].get("source", ""),
                "discovered_at": entries[0].get("discovered_at", ""),
            }
        )

        if i < len(unique_addresses):
            time.sleep(SLEEP_BETWEEN_REQUESTS)

    # Sort by score descending, filter ≥ MIN_SCORE
    results.sort(key=lambda x: x["score"], reverse=True)
    verified = [r for r in results if r["score"] >= MIN_SCORE]

    print(
        f"[analyze] {len(results)} wallets scored"
        f" → {len(verified)} passed threshold ({MIN_SCORE}+)"
    )

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_analyzed": len(results),
        "verified_count": len(verified),
        "min_score_threshold": MIN_SCORE,
        "wallets": verified,
    }
    with open(VERIFIED_WALLETS_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"[analyze] Saved {len(verified)} verified wallets → {VERIFIED_WALLETS_FILE}")
