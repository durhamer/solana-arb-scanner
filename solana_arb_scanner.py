"""
Solana DEX 套利價差掃描器 (研究用)
====================================
使用 Jupiter Quote API 比較同一交易對在不同 DEX 的報價差異
無需私鑰，純讀取，零風險

用法:
  pip install aiohttp rich --break-system-packages
  python solana_arb_scanner.py

它會持續掃描以下交易對在 Raydium / Orca / Meteora 等 DEX 之間的價差
"""

import asyncio
import aiohttp
import time
import json
import csv
import os
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

# ============================================================
# 設定區
# ============================================================

# Jupiter 公共 API (免費, 有 rate limit)
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"

# 常用 Solana 代幣 Mint 地址
TOKENS = {
    "SOL":  "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "RAY":  "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "ORCA": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",
    "JUP":  "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
}

# 要掃描的交易對 (input_token, output_token, 交易金額_以input計)
SCAN_PAIRS = [
    ("SOL",  "USDC", 1.0),      # 1 SOL → USDC
    ("SOL",  "USDT", 1.0),      # 1 SOL → USDT
    ("USDC", "USDT", 1000.0),   # 1000 USDC → USDT (穩定幣對)
]

# DEX 分層：T1 = 主流集中流動性池，T2 = 一般 AMM 池
DEX_T1 = {"Raydium CLMM", "Orca V2", "Meteora DLMM", "Whirlpool"}
DEX_T2 = {"Raydium", "Meteora", "Orca"}
ALL_TARGET_DEXES = list(DEX_T1 | DEX_T2)

# 掃描間隔 (秒) — 太快會被 rate limit
SCAN_INTERVAL = 8

# 每對之間的間隔 (秒) — 避免 rate limit
PAIR_SLEEP = 1

# 價差門檻 (%) — 超過這個值才會標記為有意義
SPREAD_ALERT_THRESHOLD = 0.3

# 日誌檔
LOG_FILE = "arb_scan_log.csv"

# token 精度 (decimals)
TOKEN_DECIMALS = {
    "SOL": 9, "USDC": 6, "USDT": 6, "RAY": 6,
    "ORCA": 6, "JUP": 6, "BONK": 5,
}

# ============================================================
# 資料結構
# ============================================================

@dataclass
class RouteInfo:
    """單一路由的報價結果"""
    dex_label: str
    input_token: str
    output_token: str
    in_amount: float
    out_amount: float
    price_impact_pct: float
    price: float  # output/input
    tier: str = ""  # "T1", "T2", "Jupiter", or ""

@dataclass
class SpreadResult:
    """價差分析結果"""
    pair: str
    best_route: RouteInfo
    worst_route: RouteInfo
    spread_pct: float          # 全部 DEX 的價差
    spread_tier1_pct: float    # 僅 T1 DEX 的價差
    spread_all_pct: float      # 全部 DEX 的價差 (同 spread_pct，保留兩欄方便比較)
    jupiter_route: Optional[RouteInfo]
    all_routes: list
    timestamp: str

# ============================================================
# Jupiter API 查詢
# ============================================================

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
        async with session.get(JUPITER_QUOTE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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
    in_decimals = TOKEN_DECIMALS.get(input_symbol, 6)

    in_amount = int(data.get("inAmount", 0)) / (10 ** in_decimals)
    out_amount = int(data.get("outAmount", 0)) / (10 ** out_decimals)
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
        async with session.get(JUPITER_QUOTE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    out_decimals = TOKEN_DECIMALS.get(output_symbol, 6)
    in_decimals = TOKEN_DECIMALS.get(input_symbol, 6)

    in_amount = int(data.get("inAmount", 0)) / (10 ** in_decimals)
    out_amount = int(data.get("outAmount", 0)) / (10 ** out_decimals)
    price_impact = float(data.get("priceImpactPct", 0))

    if in_amount <= 0 or out_amount <= 0:
        return None

    # 判斷分層
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


# ============================================================
# 價差分析
# ============================================================

def _calc_spread(routes: list[RouteInfo]) -> float:
    """計算一組路由的 best vs worst 價差百分比"""
    if len(routes) < 2:
        return 0.0
    best_out = max(r.out_amount for r in routes)
    worst_out = min(r.out_amount for r in routes)
    if worst_out <= 0:
        return 0.0
    return ((best_out - worst_out) / worst_out) * 100


async def scan_pair(
    session: aiohttp.ClientSession,
    input_symbol: str,
    output_symbol: str,
    amount: float,
) -> Optional[SpreadResult]:
    """掃描單一交易對在多個 DEX 的報價差異"""

    input_mint = TOKENS[input_symbol]
    output_mint = TOKENS[output_symbol]
    in_decimals = TOKEN_DECIMALS.get(input_symbol, 6)
    amount_raw = int(amount * (10 ** in_decimals))

    # 平行查詢各 DEX + Jupiter 聚合最佳路由
    tasks = []
    for dex in ALL_TARGET_DEXES:
        tasks.append(get_single_dex_quote(
            session, input_mint, output_mint, amount_raw,
            input_symbol, output_symbol, dex
        ))
    tasks.append(get_jupiter_best_quote(
        session, input_mint, output_mint, amount_raw,
        input_symbol, output_symbol
    ))

    results = await asyncio.gather(*tasks)

    # 彙整有效結果
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

    # 全部 DEX 的價差
    all_routes.sort(key=lambda x: x.out_amount, reverse=True)
    best = all_routes[0]
    worst = all_routes[-1]
    spread_all = _calc_spread(all_routes)

    # 僅 T1 DEX 的價差
    t1_routes = [r for r in all_routes if r.tier == "T1"]
    spread_t1 = _calc_spread(t1_routes) if len(t1_routes) >= 2 else 0.0

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
    )


# ============================================================
# 輸出與日誌
# ============================================================

def print_result(result: SpreadResult):
    """印出掃描結果"""
    is_alert = result.spread_all_pct >= SPREAD_ALERT_THRESHOLD

    icon = "🔴" if is_alert else "⚪"
    print(f"\n{icon} {result.pair} | 主流(T1)價差: {result.spread_tier1_pct:.4f}%  全部價差: {result.spread_all_pct:.4f}%")

    # Jupiter 基準
    if result.jupiter_route:
        jr = result.jupiter_route
        print(f"   [Jupiter] {jr.dex_label:40s} → {jr.out_amount:.6f} {jr.output_token} (impact: {jr.price_impact_pct:.4f}%)")

    print(f"   最佳: [{result.best_route.tier}] {result.best_route.dex_label:25s} → {result.best_route.out_amount:.6f} {result.best_route.output_token} (impact: {result.best_route.price_impact_pct:.4f}%)")
    print(f"   最差: [{result.worst_route.tier}] {result.worst_route.dex_label:25s} → {result.worst_route.out_amount:.6f} {result.worst_route.output_token} (impact: {result.worst_route.price_impact_pct:.4f}%)")

    if len(result.all_routes) > 2:
        print(f"   全部 ({len(result.all_routes)} 條路由):")
        for r in result.all_routes:
            marker = "★" if r.dex_label == result.best_route.dex_label else " "
            tier_tag = f"[{r.tier}]" if r.tier else "    "
            print(f"     {marker} {tier_tag} {r.dex_label:25s} → {r.out_amount:.6f} {r.output_token}")

    if is_alert:
        net_profit = result.best_route.out_amount - result.worst_route.out_amount
        print(f"   💰 理論毛利差: {net_profit:.6f} {result.best_route.output_token}")
        print(f"   ⚠️  注意: 實際執行還需考慮 slippage、tx fee、MEV 搶跑")


def log_to_csv(result: SpreadResult):
    """記錄到 CSV 供後續分析"""
    file_exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "timestamp", "pair",
                "spread_tier1_pct", "spread_all_pct",
                "best_dex", "best_tier", "best_out",
                "worst_dex", "worst_tier", "worst_out",
                "jupiter_out", "num_routes", "best_impact_pct"
            ])
        jupiter_out = f"{result.jupiter_route.out_amount:.8f}" if result.jupiter_route else ""
        writer.writerow([
            result.timestamp, result.pair,
            f"{result.spread_tier1_pct:.6f}", f"{result.spread_all_pct:.6f}",
            result.best_route.dex_label, result.best_route.tier, f"{result.best_route.out_amount:.8f}",
            result.worst_route.dex_label, result.worst_route.tier, f"{result.worst_route.out_amount:.8f}",
            jupiter_out, len(result.all_routes), f"{result.best_route.price_impact_pct:.6f}"
        ])


# ============================================================
# 統計摘要
# ============================================================

class ScanStats:
    def __init__(self):
        self.total_scans = 0
        self.alerts = 0
        self.max_spread = 0.0
        self.max_spread_pair = ""
        self.spreads_by_pair: dict[str, list[float]] = {}
        self.t1_spreads_by_pair: dict[str, list[float]] = {}

    def update(self, result: SpreadResult):
        self.total_scans += 1
        pair = result.pair
        if pair not in self.spreads_by_pair:
            self.spreads_by_pair[pair] = []
            self.t1_spreads_by_pair[pair] = []
        self.spreads_by_pair[pair].append(result.spread_all_pct)
        self.t1_spreads_by_pair[pair].append(result.spread_tier1_pct)

        if result.spread_all_pct >= SPREAD_ALERT_THRESHOLD:
            self.alerts += 1
        if result.spread_all_pct > self.max_spread:
            self.max_spread = result.spread_all_pct
            self.max_spread_pair = pair

    def print_summary(self):
        print("\n" + "=" * 65)
        print("📊 累計統計")
        print(f"   掃描次數: {self.total_scans}")
        print(f"   超過門檻 ({SPREAD_ALERT_THRESHOLD}%) 次數: {self.alerts}")
        print(f"   最大全部價差: {self.max_spread:.4f}% ({self.max_spread_pair})")
        print()
        print(f"   {'交易對':<15} {'T1均':>8} {'T1最大':>8} {'全部均':>8} {'全部最大':>10} {'樣本':>6}")
        print(f"   {'-'*60}")
        for pair in self.spreads_by_pair:
            all_s = self.spreads_by_pair[pair]
            t1_s = self.t1_spreads_by_pair[pair]
            avg_all = sum(all_s) / len(all_s) if all_s else 0
            max_all = max(all_s) if all_s else 0
            avg_t1 = sum(t1_s) / len(t1_s) if t1_s else 0
            max_t1 = max(t1_s) if t1_s else 0
            print(f"   {pair:<15} {avg_t1:>7.4f}% {max_t1:>7.4f}% {avg_all:>7.4f}% {max_all:>9.4f}% {len(all_s):>6}")
        print("=" * 65)


# ============================================================
# 主程式
# ============================================================

async def main():
    print("=" * 65)
    print("🔍 Solana DEX 套利價差掃描器")
    print(f"   交易對: {len(SCAN_PAIRS)} 組  (SOL/USDC, SOL/USDT, USDC/USDT)")
    print(f"   T1 DEX: {', '.join(sorted(DEX_T1))}")
    print(f"   T2 DEX: {', '.join(sorted(DEX_T2))}")
    print(f"   掃描間隔: {SCAN_INTERVAL} 秒 | 每對間隔: {PAIR_SLEEP} 秒")
    print(f"   價差門檻: {SPREAD_ALERT_THRESHOLD}%")
    print(f"   日誌: {LOG_FILE}")
    print("   Ctrl+C 停止")
    print("=" * 65)

    stats = ScanStats()
    scan_count = 0

    async with aiohttp.ClientSession() as session:
        try:
            while True:
                scan_count += 1
                print(f"\n{'─' * 45}")
                print(f"⏱️  掃描 #{scan_count} | {datetime.now().strftime('%H:%M:%S')}")
                print(f"{'─' * 45}")

                for i, (input_sym, output_sym, amount) in enumerate(SCAN_PAIRS):
                    result = await scan_pair(session, input_sym, output_sym, amount)
                    if result:
                        print_result(result)
                        log_to_csv(result)
                        stats.update(result)

                    # 每對之間 sleep，避免 rate limit
                    if i < len(SCAN_PAIRS) - 1:
                        await asyncio.sleep(PAIR_SLEEP)

                # 每 10 輪印一次統計
                if scan_count % 10 == 0:
                    stats.print_summary()

                print(f"\n⏳ 等待 {SCAN_INTERVAL} 秒...")
                await asyncio.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n🛑 停止掃描")
            stats.print_summary()
            print(f"\n📁 完整日誌已存到: {LOG_FILE}")
            print("   你可以用 pandas 分析: pd.read_csv('arb_scan_log.csv')")


if __name__ == "__main__":
    asyncio.run(main())
