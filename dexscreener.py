"""
DexScreener API 動態交易對發現模組
"""

import asyncio
import time
import aiohttp
from typing import Optional

from config import (
    DEXSCREENER_TOKENS_URL,
    DEXSCREENER_BOOSTS_URL,
    DEXSCREENER_SLEEP,
    MIN_LIQUIDITY_USD,
    MIN_VOLUME_H24_USD,
    MIN_SPREAD_PCT,
    MIN_PAIR_AGE_HOURS,
    DISCOVER_BASE_TOKENS,
    TOKENS,
)
from models import DiscoveredPair


def _parse_float(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _is_solana_pair(pair: dict) -> bool:
    return pair.get("chainId", "").lower() == "solana"


def _pair_age_hours(pair: dict) -> float:
    created_at = pair.get("pairCreatedAt")
    if not created_at:
        return 0.0
    # pairCreatedAt is Unix timestamp in milliseconds
    age_ms = time.time() * 1000 - int(created_at)
    return age_ms / (1000 * 3600)


async def _fetch_token_pairs(
    session: aiohttp.ClientSession,
    token_address: str,
) -> list[dict]:
    """呼叫 DexScreener /latest/dex/tokens/{address}，回傳 pairs 陣列"""
    url = f"{DEXSCREENER_TOKENS_URL}/{token_address}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("pairs") or []
    except Exception as e:
        print(f"  ❌ DexScreener fetch failed for {token_address[:8]}…: {e}")
        return []


async def _fetch_boosted_tokens(session: aiohttp.ClientSession) -> list[str]:
    """
    呼叫 DexScreener /token-boosts/top/v1，回傳熱門代幣的 mint 地址清單
    (只取 Solana 上的)
    """
    try:
        async with session.get(
            DEXSCREENER_BOOSTS_URL, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception as e:
        print(f"  ❌ DexScreener boosts fetch failed: {e}")
        return []

    addresses = []
    # API 回傳的格式是陣列，每個元素有 tokenAddress, chainId
    items = data if isinstance(data, list) else data.get("tokenBoosts", [])
    for item in items:
        chain = item.get("chainId", "").lower()
        addr  = item.get("tokenAddress", "")
        if chain == "solana" and addr:
            addresses.append(addr)
    return addresses


def _find_cross_dex_spreads(pairs: list[dict]) -> list[DiscoveredPair]:
    """
    給定同一 token 在多個 DEX 的池子清單，
    找出在不同 DEX 之間 priceUsd 有顯著價差的組合
    """
    # 先過濾掉不符合條件的池子
    valid = []
    for p in pairs:
        if not _is_solana_pair(p):
            continue
        liq   = _parse_float((p.get("liquidity") or {}).get("usd"))
        vol   = _parse_float((p.get("volume") or {}).get("h24"))
        price = _parse_float(p.get("priceUsd"))
        age   = _pair_age_hours(p)

        if liq < MIN_LIQUIDITY_USD:
            continue
        if vol < MIN_VOLUME_H24_USD:
            continue
        if price <= 0:
            continue
        if age < MIN_PAIR_AGE_HOURS:
            continue

        valid.append(p)

    if len(valid) < 2:
        return []

    discovered: list[DiscoveredPair] = []

    # 對每對池子計算價差（O(n²)，池子數量通常很少，沒問題）
    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            pa = valid[i]
            pb = valid[j]

            dex_a = pa.get("dexId", "unknown")
            dex_b = pb.get("dexId", "unknown")
            if dex_a == dex_b:
                continue  # 同一 DEX 不比

            price_a = _parse_float(pa.get("priceUsd"))
            price_b = _parse_float(pb.get("priceUsd"))

            low, high = (price_a, price_b) if price_a < price_b else (price_b, price_a)
            spread_pct = (high - low) / low * 100 if low > 0 else 0.0

            if spread_pct < MIN_SPREAD_PCT:
                continue

            # 取 baseToken 資訊（優先用 pa 的）
            base = pa.get("baseToken") or {}
            symbol  = base.get("symbol", "?")
            address = base.get("address", "")

            liq_a = _parse_float((pa.get("liquidity") or {}).get("usd"))
            liq_b = _parse_float((pb.get("liquidity") or {}).get("usd"))
            vol_a = _parse_float((pa.get("volume") or {}).get("h24"))
            vol_b = _parse_float((pb.get("volume") or {}).get("h24"))

            discovered.append(DiscoveredPair(
                token_symbol=symbol,
                token_address=address,
                dex_a=dex_a,
                dex_b=dex_b,
                price_a=price_a,
                price_b=price_b,
                spread_pct=spread_pct,
                liquidity_a=liq_a,
                liquidity_b=liq_b,
                volume_h24_a=vol_a,
                volume_h24_b=vol_b,
            ))

    return discovered


async def discover_arbitrage_pairs(
    session: aiohttp.ClientSession,
) -> list[DiscoveredPair]:
    """
    主入口：
    1. 拉取 SOL / USDC 的所有 Solana 池子
    2. 拉取熱門代幣 (top boosts) 的池子
    3. 對每個 token 找跨 DEX 價差
    4. 回傳符合條件的 DiscoveredPair 清單
    """
    all_discovered: list[DiscoveredPair] = []
    seen_token_addrs: set[str] = set()

    # ── Step 1: 固定基礎 token (SOL, USDC) ──────────────────────
    for addr in DISCOVER_BASE_TOKENS:
        pairs = await _fetch_token_pairs(session, addr)
        found = _find_cross_dex_spreads(pairs)
        all_discovered.extend(found)
        seen_token_addrs.add(addr)
        await asyncio.sleep(DEXSCREENER_SLEEP)

    # ── Step 2: 熱門 token (boosts) ─────────────────────────────
    boosted_addrs = await _fetch_boosted_tokens(session)
    await asyncio.sleep(DEXSCREENER_SLEEP)

    for addr in boosted_addrs:
        if addr in seen_token_addrs:
            continue
        seen_token_addrs.add(addr)

        pairs = await _fetch_token_pairs(session, addr)
        found = _find_cross_dex_spreads(pairs)
        all_discovered.extend(found)
        await asyncio.sleep(DEXSCREENER_SLEEP)

    # 去重：同一 (symbol, dex_a, dex_b) 只保留價差最大的
    deduped: dict[tuple, DiscoveredPair] = {}
    for dp in all_discovered:
        key = (dp.token_symbol, frozenset([dp.dex_a, dp.dex_b]))
        if key not in deduped or dp.spread_pct > deduped[key].spread_pct:
            deduped[key] = dp

    result = sorted(deduped.values(), key=lambda x: x.spread_pct, reverse=True)
    return result


def print_discovered(pairs: list[DiscoveredPair]):
    """印出動態發現的套利機會"""
    if not pairs:
        print("  (未發現符合條件的動態交易對)")
        return
    print(f"  發現 {len(pairs)} 個跨 DEX 套利機會:")
    print(f"  {'Token':<10} {'DEX A':<20} {'DEX B':<20} {'價A':>10} {'價B':>10} {'價差%':>8} {'流動性A':>12} {'流動性B':>12}")
    print(f"  {'-'*104}")
    for dp in pairs:
        print(
            f"  {dp.token_symbol:<10} {dp.dex_a:<20} {dp.dex_b:<20} "
            f"${dp.price_a:>9.6f} ${dp.price_b:>9.6f} {dp.spread_pct:>7.3f}% "
            f"${dp.liquidity_a:>11,.0f} ${dp.liquidity_b:>11,.0f}"
        )
