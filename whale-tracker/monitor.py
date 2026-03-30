"""
Real-time whale wallet monitor.

Polls verified_wallets.json wallets with score >= 85 every 30 seconds via
Helius Enhanced Transactions API and fires alerts on:
  🚨 CRITICAL — multiple tier-1 wallets buying the same token within 1 hour
  🔴 HIGH     — a tier-1 wallet buys a token it has never bought before
  ⚠️  WARNING  — a tier-1 wallet executes a large sell (>= LARGE_SELL_SOL)

Alerts are written to alerts.json and optionally emailed via notifier.py.
"""

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import requests

from config import HELIUS_API_KEY
from notifier import send_alert_email

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERIFIED_WALLETS_FILE = "verified_wallets.json"
ALERTS_FILE = "alerts.json"

MIN_SCORE = 85               # tier-1 threshold
POLL_INTERVAL = 30           # seconds between full poll cycles
HELIUS_SLEEP = 0.5           # seconds between individual Helius requests
SNAPSHOT_LIMIT = 50          # transactions fetched for initial position snapshot
POLL_LIMIT = 5               # transactions fetched per wallet per poll cycle
LARGE_SELL_SOL = 5.0         # SOL received threshold for large-sell alert
LARGE_SELL_USDC = 750.0      # USDC received threshold for large-sell alert (~5 SOL)
MULTI_WALLET_WINDOW = 3600   # seconds (1 h) for multi-wallet convergence check

WSOL = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: Optional[int]) -> str:
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _shorten(address: str, n: int = 6) -> str:
    return f"{address[:n]}...{address[-4:]}"


def _mint_label(mint: str) -> str:
    """Return a short human-readable label for a mint address."""
    return f"{mint[:6]}...{mint[-4:]}"


def _fetch_transactions(address: str, limit: int) -> list[dict]:
    url = HELIUS_TX_URL.format(address=address)
    params = {"api-key": HELIUS_API_KEY, "type": "SWAP", "limit": limit}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  [warn] fetch failed for {_shorten(address)}: {e}")
        return []


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def _build_snapshot(address: str) -> tuple[set[str], str]:
    """
    Fetch the last SNAPSHOT_LIMIT swap transactions for a wallet.

    Returns:
        (holdings, last_sig)
        holdings  — set of token mints the wallet has previously bought
        last_sig  — most recent transaction signature (used to skip replays)
    """
    txs = _fetch_transactions(address, limit=SNAPSHOT_LIMIT)
    holdings: set[str] = set()
    last_sig = txs[0].get("signature", "") if txs else ""

    for tx in txs:
        swap = tx.get("events", {}).get("swap", {})
        if swap:
            for t in swap.get("tokenOutputs", []):
                mint = t.get("mint", "")
                if mint and mint != WSOL:
                    holdings.add(mint)
        # Fallback: tokenTransfers received by this wallet
        for transfer in tx.get("tokenTransfers", []):
            if transfer.get("toUserAccount") == address:
                mint = transfer.get("mint", "")
                if mint and mint != WSOL:
                    holdings.add(mint)

    return holdings, last_sig


# ---------------------------------------------------------------------------
# Transaction parsing
# ---------------------------------------------------------------------------

def _parse_swap(tx: dict, wallet_address: str) -> Optional[dict]:
    """
    Decode a Helius enhanced SWAP transaction into a structured dict.
    Returns None when the transaction carries no useful token movement.
    """
    sig = tx.get("signature", "")
    ts = tx.get("timestamp")
    dex = tx.get("source", "UNKNOWN")
    swap = tx.get("events", {}).get("swap", {})

    bought: list[dict] = []
    sold: list[dict] = []

    if swap:
        for t in swap.get("tokenOutputs", []):
            mint = t.get("mint", "")
            if mint and mint != WSOL:
                bought.append({
                    "mint": mint,
                    "amount": t.get("tokenAmount", 0),
                    "symbol": _mint_label(mint),
                })
        for t in swap.get("tokenInputs", []):
            mint = t.get("mint", "")
            if mint and mint != WSOL:
                sold.append({
                    "mint": mint,
                    "amount": t.get("tokenAmount", 0),
                    "symbol": _mint_label(mint),
                })
    else:
        # Fallback: directional tokenTransfers
        for transfer in tx.get("tokenTransfers", []):
            mint = transfer.get("mint", "")
            if not mint or mint == WSOL:
                continue
            amount = transfer.get("tokenAmount", 0)
            if transfer.get("toUserAccount") == wallet_address:
                bought.append({"mint": mint, "amount": amount, "symbol": _mint_label(mint)})
            elif transfer.get("fromUserAccount") == wallet_address:
                sold.append({"mint": mint, "amount": amount, "symbol": _mint_label(mint)})

    if not bought and not sold:
        return None

    # SOL amounts from structured swap event
    sol_spent = 0.0
    sol_received = 0.0
    if swap:
        native_in = swap.get("nativeInput") or {}
        if native_in.get("account") == wallet_address:
            sol_spent = int(native_in.get("amount", 0)) / 1e9
        native_out = swap.get("nativeOutput") or {}
        if native_out.get("account") == wallet_address:
            sol_received = int(native_out.get("amount", 0)) / 1e9

    # Fallback: accountData nativeBalanceChange
    if sol_spent == 0.0 and sol_received == 0.0:
        for acct in tx.get("accountData", []):
            if acct.get("account") == wallet_address:
                change = acct.get("nativeBalanceChange", 0)
                if change < 0:
                    sol_spent = abs(change) / 1e9
                elif change > 0:
                    sol_received = change / 1e9
                break

    # USDC amounts from tokenTransfers
    usdc_spent = 0.0
    usdc_received = 0.0
    for transfer in tx.get("tokenTransfers", []):
        if transfer.get("mint") != USDC_MINT:
            continue
        amt = transfer.get("tokenAmount", 0)
        if transfer.get("fromUserAccount") == wallet_address:
            usdc_spent += amt
        elif transfer.get("toUserAccount") == wallet_address:
            usdc_received += amt

    return {
        "signature": sig,
        "timestamp": ts,
        "dex": dex,
        "bought": bought,
        "sold": sold,
        "sol_spent": round(sol_spent, 4),
        "sol_received": round(sol_received, 4),
        "usdc_spent": round(usdc_spent, 4),
        "usdc_received": round(usdc_received, 4),
    }


# ---------------------------------------------------------------------------
# Alert helpers
# ---------------------------------------------------------------------------

def _cost_str(sol: float, usdc: float) -> str:
    if sol > 0:
        return f"{sol:.4f} SOL"
    if usdc > 0:
        return f"{usdc:.2f} USDC"
    return "unknown amount"


def _print_alert(priority: str, message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {priority} {message}")


def _append_alert(alert: dict) -> None:
    try:
        with open(ALERTS_FILE) as f:
            alerts = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        alerts = []
    alerts.append(alert)
    with open(ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2)


def _build_email_body(alert: dict, extra_wallets: Optional[set[str]] = None) -> str:
    sig = alert.get("tx_signature", "")
    tx_link = f"https://solscan.io/tx/{sig}" if sig else "N/A"
    lines = [
        f"Wallet:        {alert.get('wallet', '')}",
        f"Action:        {alert.get('action', '')}",
        f"Token:         {alert.get('token', '')}",
        f"Token Address: {alert.get('token_address', '')}",
        f"Amount:        {alert.get('amount', '')}",
        f"DEX:           {alert.get('dex', '')}",
        f"Time:          {alert.get('timestamp', '')}",
        f"Transaction:   {tx_link}",
    ]
    if extra_wallets:
        lines.append(f"All buyers:    {', '.join(sorted(extra_wallets))}")
    return "\n".join(lines)


def _fire_alert(
    priority_emoji: str,
    priority_label: str,
    email_prefix: str,
    email_subject_suffix: str,
    terminal_msg: str,
    alert: dict,
    extra_wallets: Optional[set[str]] = None,
) -> None:
    _print_alert(priority_emoji, terminal_msg)
    _append_alert(alert)
    subject = f"{email_prefix} {email_subject_suffix}"
    body = _build_email_body(alert, extra_wallets)
    send_alert_email(subject, body)


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------

def _load_verified_wallets() -> list[dict]:
    try:
        with open(VERIFIED_WALLETS_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[error] {VERIFIED_WALLETS_FILE} not found. Run 'python main.py analyze' first.")
        return []
    return [w for w in data.get("wallets", []) if w.get("score", 0) >= MIN_SCORE]


def run_monitor() -> None:
    if not HELIUS_API_KEY:
        print("[error] HELIUS_API_KEY environment variable not set.")
        return

    wallets = _load_verified_wallets()
    if not wallets:
        print(f"[monitor] No wallets with score >= {MIN_SCORE}. Exiting.")
        return

    print(f"[monitor] {len(wallets)} tier-1 wallets loaded (score >= {MIN_SCORE})")
    print("[monitor] Building initial position snapshots...\n")

    # Per-wallet state
    holdings: dict[str, set[str]] = {}   # address -> set of mints ever bought
    last_seen: dict[str, str] = {}        # address -> most recent tx signature

    for i, w in enumerate(wallets, 1):
        addr = w["address"]
        short = _shorten(addr)
        print(f"  [{i}/{len(wallets)}] {short} — fetching snapshot...")
        snap, last_sig = _build_snapshot(addr)
        holdings[addr] = snap
        last_seen[addr] = last_sig
        print(f"    {len(snap)} previously traded tokens | last_sig={last_sig[:12] if last_sig else 'none'}...")
        if i < len(wallets):
            time.sleep(HELIUS_SLEEP)

    # token_mint -> list of (wallet_address, unix_timestamp)
    # used to detect multi-wallet convergence
    token_buyers: dict[str, list[tuple[str, int]]] = defaultdict(list)

    print(f"\n[monitor] Poll interval: {POLL_INTERVAL}s | large-sell threshold: {LARGE_SELL_SOL} SOL\n")
    print("=" * 72)

    while True:
        cycle_start = time.time()

        for w in wallets:
            addr = w["address"]
            short = _shorten(addr)

            txs = _fetch_transactions(addr, limit=POLL_LIMIT)
            time.sleep(HELIUS_SLEEP)

            if not txs:
                continue

            # Identify transactions newer than the last seen signature
            prev_sig = last_seen.get(addr, "")
            new_txs: list[dict] = []
            for tx in txs:
                if tx.get("signature", "") == prev_sig:
                    break
                new_txs.append(tx)

            if new_txs:
                last_seen[addr] = txs[0].get("signature", "")

            # Process oldest-first so chronological order is preserved
            for tx in reversed(new_txs):
                parsed = _parse_swap(tx, addr)
                if not parsed:
                    continue

                sig = parsed["signature"]
                ts = parsed["timestamp"]
                dex = parsed["dex"]

                # ── BUY events ──────────────────────────────────────────────
                for token in parsed["bought"]:
                    mint = token["mint"]
                    symbol = token["symbol"]
                    cost = _cost_str(parsed["sol_spent"], parsed["usdc_spent"])
                    is_new = mint not in holdings[addr]
                    holdings[addr].add(mint)

                    # Record buyer for multi-wallet check
                    event_ts = ts if ts else int(time.time())
                    token_buyers[mint].append((addr, event_ts))

                    # Multi-wallet convergence: >=2 unique tier-1 wallets in window
                    now_ts = int(time.time())
                    recent = [
                        (a, t) for a, t in token_buyers[mint]
                        if now_ts - t <= MULTI_WALLET_WINDOW
                    ]
                    unique_buyers = {a for a, _ in recent}

                    if len(unique_buyers) >= 2:
                        alert = {
                            "timestamp": _fmt_ts(ts),
                            "wallet": addr,
                            "action": "multi_wallet_buy",
                            "token": symbol,
                            "token_address": mint,
                            "amount": cost,
                            "dex": dex,
                            "priority": "CRITICAL",
                            "tx_signature": sig,
                        }
                        _fire_alert(
                            priority_emoji="🚨 CRITICAL",
                            priority_label="CRITICAL",
                            email_prefix="[URGENT]",
                            email_subject_suffix=f"{len(unique_buyers)} whales buying {symbol}",
                            terminal_msg=(
                                f"{len(unique_buyers)} whales buying {symbol} ({mint[:8]}...) | "
                                f"{short} spent {cost} on {dex}"
                            ),
                            alert=alert,
                            extra_wallets=unique_buyers,
                        )

                    elif is_new:
                        alert = {
                            "timestamp": _fmt_ts(ts),
                            "wallet": addr,
                            "action": "new_token_buy",
                            "token": symbol,
                            "token_address": mint,
                            "amount": cost,
                            "dex": dex,
                            "priority": "HIGH",
                            "tx_signature": sig,
                        }
                        _fire_alert(
                            priority_emoji="🔴 HIGH",
                            priority_label="HIGH",
                            email_prefix="[ALERT]",
                            email_subject_suffix=f"Whale bought new token {symbol}",
                            terminal_msg=(
                                f"{short} bought NEW token {symbol} ({mint[:8]}...) | "
                                f"spent {cost} on {dex}"
                            ),
                            alert=alert,
                        )

                # ── SELL events ─────────────────────────────────────────────
                for token in parsed["sold"]:
                    mint = token["mint"]
                    symbol = token["symbol"]
                    sol_rx = parsed["sol_received"]
                    usdc_rx = parsed["usdc_received"]

                    if sol_rx >= LARGE_SELL_SOL or usdc_rx >= LARGE_SELL_USDC:
                        received = _cost_str(sol_rx, usdc_rx)
                        alert = {
                            "timestamp": _fmt_ts(ts),
                            "wallet": addr,
                            "action": "large_sell",
                            "token": symbol,
                            "token_address": mint,
                            "amount": received,
                            "dex": dex,
                            "priority": "WARNING",
                            "tx_signature": sig,
                        }
                        _fire_alert(
                            priority_emoji="⚠️  WARNING",
                            priority_label="WARNING",
                            email_prefix="[WARNING]",
                            email_subject_suffix=f"Whale large sell of {symbol}",
                            terminal_msg=(
                                f"{short} large SELL of {symbol} ({mint[:8]}...) | "
                                f"received {received} on {dex}"
                            ),
                            alert=alert,
                        )

        elapsed = time.time() - cycle_start
        sleep_for = max(0.0, POLL_INTERVAL - elapsed)
        if sleep_for > 0:
            time.sleep(sleep_for)
