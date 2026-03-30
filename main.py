"""
主迴圈入口，組合所有模組
用法: python main.py
"""

import asyncio
import time
import aiohttp
from datetime import datetime

from config import (
    STATIC_SCAN_PAIRS,
    SCAN_INTERVAL,
    PAIR_SLEEP,
    SPREAD_ALERT_THRESHOLD,
    LOG_FILE,
    DEX_T1,
    DEX_T2,
    DISCOVER_INTERVAL,
)
from models import DiscoveredPair
from scanner import scan_pair, scan_dynamic_pair
from dexscreener import discover_arbitrage_pairs, print_discovered
from logger import print_result, log_to_csv, ScanStats


async def run_discover(session: aiohttp.ClientSession) -> list[DiscoveredPair]:
    """執行 DexScreener 動態發現，印出結果並回傳清單"""
    print("\n🔎 DexScreener 動態發現中...")
    discovered = await discover_arbitrage_pairs(session)
    print_discovered(discovered)
    return discovered


async def main():
    print("=" * 65)
    print("🔍 Solana DEX 套利價差掃描器")
    print(f"   固定交易對: {len(STATIC_SCAN_PAIRS)} 組  (SOL/USDC, SOL/USDT, USDC/USDT)")
    print(f"   T1 DEX: {', '.join(sorted(DEX_T1))}")
    print(f"   T2 DEX: {', '.join(sorted(DEX_T2))}")
    print(f"   掃描間隔: {SCAN_INTERVAL} 秒 | 每對間隔: {PAIR_SLEEP} 秒")
    print(f"   價差門檻: {SPREAD_ALERT_THRESHOLD}%")
    print(f"   動態發現間隔: {DISCOVER_INTERVAL} 秒")
    print(f"   日誌: {LOG_FILE}")
    print("   Ctrl+C 停止")
    print("=" * 65)

    stats      = ScanStats()
    scan_count = 0

    async with aiohttp.ClientSession() as session:
        try:
            # ── 主迴圈開始前先跑一次 discover ─────────────────────
            discovered_pairs: list[DiscoveredPair] = await run_discover(session)
            last_discover_time = time.monotonic()

            while True:
                scan_count += 1
                print(f"\n{'─' * 45}")
                print(f"⏱️  掃描 #{scan_count} | {datetime.now().strftime('%H:%M:%S')}")

                # ── 每 DISCOVER_INTERVAL 秒重新 discover ───────────
                elapsed = time.monotonic() - last_discover_time
                if elapsed >= DISCOVER_INTERVAL:
                    discovered_pairs = await run_discover(session)
                    last_discover_time = time.monotonic()

                # ── 固定交易對 ─────────────────────────────────────
                print(f"\n📌 固定交易對 ({len(STATIC_SCAN_PAIRS)} 組)")
                print(f"{'─' * 45}")
                for i, (input_sym, output_sym, amount) in enumerate(STATIC_SCAN_PAIRS):
                    result = await scan_pair(session, input_sym, output_sym, amount)
                    if result:
                        print_result(result)
                        log_to_csv(result)
                        stats.update(result)

                    if i < len(STATIC_SCAN_PAIRS) - 1:
                        await asyncio.sleep(PAIR_SLEEP)

                # ── 動態發現的交易對（Jupiter 二次驗證）────────────
                if discovered_pairs:
                    print(f"\n🌐 動態發現交易對 ({len(discovered_pairs)} 組，Jupiter 二次驗證)")
                    print(f"{'─' * 45}")
                    for i, dp in enumerate(discovered_pairs):
                        result = await scan_dynamic_pair(session, dp)
                        if result:
                            print_result(result)
                            log_to_csv(result)
                            stats.update(result)

                        if i < len(discovered_pairs) - 1:
                            await asyncio.sleep(PAIR_SLEEP)

                # ── 每 10 輪印一次統計 ────────────────────────────
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
