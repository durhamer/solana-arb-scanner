"""
資料結構：RouteInfo、SpreadResult、DiscoveredPair
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RouteInfo:
    """單一路由的報價結果"""
    dex_label: str
    input_token: str
    output_token: str
    in_amount: float
    out_amount: float
    price_impact_pct: float
    price: float          # output / input
    tier: str = ""        # "T1", "T2", "Jupiter", or ""


@dataclass
class SpreadResult:
    """價差分析結果"""
    pair: str
    best_route: RouteInfo
    worst_route: RouteInfo
    spread_pct: float           # 全部 DEX 的價差
    spread_tier1_pct: float     # 僅 T1 DEX 的價差
    spread_all_pct: float       # 全部 DEX 的價差 (同 spread_pct，保留兩欄方便比較)
    jupiter_route: Optional[RouteInfo]
    all_routes: list
    timestamp: str
    is_dynamic: bool = False    # True = 來自 DexScreener 動態發現


@dataclass
class DiscoveredPair:
    """DexScreener 發現的套利機會"""
    token_symbol: str
    token_address: str
    dex_a: str
    dex_b: str
    price_a: float
    price_b: float
    spread_pct: float
    liquidity_a: float
    liquidity_b: float
    volume_h24_a: float
    volume_h24_b: float
    chain_id: str = "solana"

    @property
    def quote_token(self) -> str:
        """回傳 USDC 或 USDT 做為 quote"""
        return "USDC"

    @property
    def scan_amount(self) -> float:
        from config import DYNAMIC_PAIR_AMOUNT
        return DYNAMIC_PAIR_AMOUNT
