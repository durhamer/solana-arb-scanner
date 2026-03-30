"""
掃描邏輯：組合查詢、計算價差、分 T1/T2
"""

import asyncio
import aiohttp
from datetime import datetime
from typing import Optional

from config import TOKENS, TOKEN_DECIMALS, ALL_TARGET_DEXES
from models import RouteInfo, SpreadResult, DiscoveredPair
from jupiter_client import get_single_dex_quote, get_jupiter_best_quote


def _calc_spread(routes: list[RouteInfo]) -> float:
    """計算一組路由的 best vs worst 價差百分比"""
    if len(routes) < 2:
        return 0.0
    best_out  = max(r.out_amount for r in routes)
    worst_out = min(r.out_amount for r in routes)
    if worst_out <= 0:
        return 0.0
    return ((best_out - worst_out) / worst_out) * 100


async def scan_pair(
    session: aiohttp.ClientSession,
    input_symbol: str,
    output_symbol: str,
    amount: float,
    is_dynamic: bool = False,
) -> Optional[SpreadResult]:
    """掃描單一交易對在多個 DEX 的報價差異"""

    input_mint  = TOKENS.get(input_symbol)
    output_mint = TOKENS.get(output_symbol)

    # 動態發現的 token 可能不在 TOKENS 字典裡，用 address 直接傳進來
    if not input_mint:
        input_mint = input_symbol   # 允許直接傳 mint address
    if not output_mint:
        output_mint = output_symbol

    in_decimals = TOKEN_DECIMALS.get(input_symbol, 6)
    amount_raw  = int(amount * (10 ** in_decimals))

    # 平行查詢各 DEX + Jupiter 聚合最佳路由
    tasks = [
        get_single_dex_quote(
            session, input_mint, output_mint, amount_raw,
            input_symbol, output_symbol, dex
        )
        for dex in ALL_TARGET_DEXES
    ]
    tasks.append(get_jupiter_best_quote(
        session, input_mint, output_mint, amount_raw,
        input_symbol, output_symbol
    ))

    results = await asyncio.gather(*tasks)

    all_routes: list[RouteInfo] = []
    jupiter_route: Optional[RouteInfo] = None

    for r in results:
        if r is None:
            continue
        if r.tier == "Jupiter":
            jupiter_route = r
        else:
            all_routes.append(r)

    if len(all_routes) < 2:
        return None

    all_routes.sort(key=lambda x: x.out_amount, reverse=True)
    best   = all_routes[0]
    worst  = all_routes[-1]
    spread_all = _calc_spread(all_routes)

    t1_routes  = [r for r in all_routes if r.tier == "T1"]
    spread_t1  = _calc_spread(t1_routes) if len(t1_routes) >= 2 else 0.0

    return SpreadResult(
        pair=f"{input_symbol}/{output_symbol}",
        best_route=best,
        worst_route=worst,
        spread_pct=spread_all,
        spread_tier1_pct=spread_t1,
        spread_all_pct=spread_all,
        jupiter_route=jupiter_route,
        all_routes=all_routes,
        timestamp=datetime.now().isoformat(),
        is_dynamic=is_dynamic,
    )


async def scan_dynamic_pair(
    session: aiohttp.ClientSession,
    dp: DiscoveredPair,
) -> Optional[SpreadResult]:
    """
    對 DexScreener 發現的交易對做 Jupiter 二次驗證
    input = dp.token_address (Solana mint)
    output = USDC
    """
    from config import TOKENS, TOKEN_DECIMALS, DYNAMIC_PAIR_AMOUNT, DYNAMIC_PAIR_QUOTE

    output_symbol = DYNAMIC_PAIR_QUOTE
    output_mint   = TOKENS[output_symbol]
    in_decimals   = 9   # 大多數 SPL token 用 9，若有特殊的之後再擴充
    amount_raw    = int(DYNAMIC_PAIR_AMOUNT * (10 ** in_decimals))

    tasks = [
        get_single_dex_quote(
            session,
            dp.token_address, output_mint,
            amount_raw,
            dp.token_symbol, output_symbol,
            dex,
        )
        for dex in ALL_TARGET_DEXES
    ]
    tasks.append(get_jupiter_best_quote(
        session,
        dp.token_address, output_mint,
        amount_raw,
        dp.token_symbol, output_symbol,
    ))

    results = await asyncio.gather(*tasks)

    all_routes: list[RouteInfo] = []
    jupiter_route: Optional[RouteInfo] = None

    for r in results:
        if r is None:
            continue
        if r.tier == "Jupiter":
            jupiter_route = r
        else:
            all_routes.append(r)

    if len(all_routes) < 2:
        return None

    all_routes.sort(key=lambda x: x.out_amount, reverse=True)
    best  = all_routes[0]
    worst = all_routes[-1]
    spread_all = _calc_spread(all_routes)
    t1_routes  = [r for r in all_routes if r.tier == "T1"]
    spread_t1  = _calc_spread(t1_routes) if len(t1_routes) >= 2 else 0.0

    return SpreadResult(
        pair=f"{dp.token_symbol}/{output_symbol}",
        best_route=best,
        worst_route=worst,
        spread_pct=spread_all,
        spread_tier1_pct=spread_t1,
        spread_all_pct=spread_all,
        jupiter_route=jupiter_route,
        all_routes=all_routes,
        timestamp=datetime.now().isoformat(),
        is_dynamic=True,
    )
