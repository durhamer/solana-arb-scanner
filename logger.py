"""
CSV 記錄 + 統計摘要
"""

import csv
import os

from config import LOG_FILE, SPREAD_ALERT_THRESHOLD
from models import SpreadResult


def print_result(result: SpreadResult):
    """印出掃描結果"""
    is_alert = result.spread_all_pct >= SPREAD_ALERT_THRESHOLD
    icon     = "🔴" if is_alert else "⚪"
    tag      = " [動態]" if result.is_dynamic else ""

    print(f"\n{icon}{tag} {result.pair} | 主流(T1)價差: {result.spread_tier1_pct:.4f}%  全部價差: {result.spread_all_pct:.4f}%")

    if result.jupiter_route:
        jr = result.jupiter_route
        print(f"   [Jupiter] {jr.dex_label:40s} → {jr.out_amount:.6f} {jr.output_token} (impact: {jr.price_impact_pct:.4f}%)")

    print(f"   最佳: [{result.best_route.tier}] {result.best_route.dex_label:25s} → {result.best_route.out_amount:.6f} {result.best_route.output_token} (impact: {result.best_route.price_impact_pct:.4f}%)")
    print(f"   最差: [{result.worst_route.tier}] {result.worst_route.dex_label:25s} → {result.worst_route.out_amount:.6f} {result.worst_route.output_token} (impact: {result.worst_route.price_impact_pct:.4f}%)")

    if len(result.all_routes) > 2:
        print(f"   全部 ({len(result.all_routes)} 條路由):")
        for r in result.all_routes:
            marker   = "★" if r.dex_label == result.best_route.dex_label else " "
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
                "timestamp", "pair", "is_dynamic",
                "spread_tier1_pct", "spread_all_pct",
                "best_dex", "best_tier", "best_out",
                "worst_dex", "worst_tier", "worst_out",
                "jupiter_out", "num_routes", "best_impact_pct",
            ])
        jupiter_out = f"{result.jupiter_route.out_amount:.8f}" if result.jupiter_route else ""
        writer.writerow([
            result.timestamp, result.pair, int(result.is_dynamic),
            f"{result.spread_tier1_pct:.6f}", f"{result.spread_all_pct:.6f}",
            result.best_route.dex_label, result.best_route.tier, f"{result.best_route.out_amount:.8f}",
            result.worst_route.dex_label, result.worst_route.tier, f"{result.worst_route.out_amount:.8f}",
            jupiter_out, len(result.all_routes), f"{result.best_route.price_impact_pct:.6f}",
        ])


class ScanStats:
    def __init__(self):
        self.total_scans = 0
        self.alerts      = 0
        self.max_spread  = 0.0
        self.max_spread_pair = ""
        self.spreads_by_pair:    dict[str, list[float]] = {}
        self.t1_spreads_by_pair: dict[str, list[float]] = {}

    def update(self, result: SpreadResult):
        self.total_scans += 1
        pair = result.pair
        if pair not in self.spreads_by_pair:
            self.spreads_by_pair[pair]    = []
            self.t1_spreads_by_pair[pair] = []
        self.spreads_by_pair[pair].append(result.spread_all_pct)
        self.t1_spreads_by_pair[pair].append(result.spread_tier1_pct)

        if result.spread_all_pct >= SPREAD_ALERT_THRESHOLD:
            self.alerts += 1
        if result.spread_all_pct > self.max_spread:
            self.max_spread      = result.spread_all_pct
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
            t1_s  = self.t1_spreads_by_pair[pair]
            avg_all = sum(all_s) / len(all_s) if all_s else 0
            max_all = max(all_s) if all_s else 0
            avg_t1  = sum(t1_s) / len(t1_s) if t1_s else 0
            max_t1  = max(t1_s) if t1_s else 0
            print(f"   {pair:<15} {avg_t1:>7.4f}% {max_t1:>7.4f}% {avg_all:>7.4f}% {max_all:>9.4f}% {len(all_s):>6}")
        print("=" * 65)
