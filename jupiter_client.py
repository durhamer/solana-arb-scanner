"""
Jupiter API 查詢邏輯：單 DEX 報價、最佳路由
"""

import asyncio
import aiohttp
from typing import Optional

from config import JUPITER_QUOTE_URL, TOKEN_DECIMALS, DEX_T1, DEX_T2
from models import RouteInfo


async def get_jupiter_best_quote(
    session: aiohttp.ClientSession,
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    input_symbol: str,
    output_symbol: str,
) -> Optional[RouteInfo]:
    """
    從 Jupiter 取得聚合最佳路由 (不限定 DEX)，作為基準參考
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": "50",
        "maxAccounts": "64",
    }

    try:
        async with session.get(
            JUPITER_QUOTE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 429:
                print("  ⚠️  Rate limited, waiting...")
                await asyncio.sleep(3)
                return None
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as e:
        print(f"  ❌ Jupiter best quote failed: {e}")
        return None

    route_plan = data.get("routePlan", [])
    if not route_plan:
        return None

    dex_labels = [step.get("swapInfo", {}).get("label", "Unknown") for step in route_plan]
    dex_path = " → ".join(dex_labels)

    out_decimals = TOKEN_DECIMALS.get(output_symbol, 6)
    in_decimals  = TOKEN_DECIMALS.get(input_symbol, 6)

    in_amount    = int(data.get("inAmount", 0))  / (10 ** in_decimals)
    out_amount   = int(data.get("outAmount", 0)) / (10 ** out_decimals)
    price_impact = float(data.get("priceImpactPct", 0))

    if in_amount <= 0 or out_amount <= 0:
        return None

    return RouteInfo(
        dex_label=f"Jupiter({dex_path})",
        input_token=input_symbol,
        output_token=output_symbol,
        in_amount=in_amount,
        out_amount=out_amount,
        price_impact_pct=price_impact,
        price=out_amount / in_amount,
        tier="Jupiter",
    )


async def get_single_dex_quote(
    session: aiohttp.ClientSession,
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    input_symbol: str,
    output_symbol: str,
    dex_label: str,
) -> Optional[RouteInfo]:
    """
    用 Jupiter 但限定只走特定 DEX (透過 dexes 參數)
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": "50",
        "onlyDirectRoutes": "true",
        "dexes": dex_label,
    }

    try:
        async with session.get(
            JUPITER_QUOTE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    out_decimals = TOKEN_DECIMALS.get(output_symbol, 6)
    in_decimals  = TOKEN_DECIMALS.get(input_symbol, 6)

    in_amount    = int(data.get("inAmount", 0))  / (10 ** in_decimals)
    out_amount   = int(data.get("outAmount", 0)) / (10 ** out_decimals)
    price_impact = float(data.get("priceImpactPct", 0))

    if in_amount <= 0 or out_amount <= 0:
        return None

    if dex_label in DEX_T1:
        tier = "T1"
    elif dex_label in DEX_T2:
        tier = "T2"
    else:
        tier = ""

    return RouteInfo(
        dex_label=dex_label,
        input_token=input_symbol,
        output_token=output_symbol,
        in_amount=in_amount,
        out_amount=out_amount,
        price_impact_pct=price_impact,
        price=out_amount / in_amount,
        tier=tier,
    )
