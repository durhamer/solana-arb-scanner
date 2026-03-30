"""
所有常數設定：token 地址、decimals、門檻值、間隔時間
"""

# Jupiter 公共 API (免費, 有 rate limit)
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"

# DexScreener API
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens"
DEXSCREENER_BOOSTS_URL  = "https://api.dexscreener.com/token-boosts/top/v1"

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

# token 精度 (decimals)
TOKEN_DECIMALS = {
    "SOL": 9, "USDC": 6, "USDT": 6, "RAY": 6,
    "ORCA": 6, "JUP": 6, "BONK": 5,
}

# 固定掃描的交易對 (input_token, output_token, 交易金額_以input計)
STATIC_SCAN_PAIRS = [
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

# ── DexScreener 動態發現設定 ──────────────────────────────────

# 重新 discover 的間隔 (秒)
DISCOVER_INTERVAL = 300  # 5 分鐘

# DexScreener 請求之間的 sleep，避免 rate limit
DEXSCREENER_SLEEP = 1.0

# 池子過濾條件
MIN_LIQUIDITY_USD   = 50_000    # 最低流動性 $50k
MIN_VOLUME_H24_USD  = 10_000    # 最低 24h 交易量 $10k
MIN_SPREAD_PCT      = 0.3       # 最低價差 0.3%
MIN_PAIR_AGE_HOURS  = 24        # 池子至少存在 24 小時

# 動態發現：要查詢的基礎 token（用逗號一次查多個）
DISCOVER_BASE_TOKENS = [
    TOKENS["SOL"],
    TOKENS["USDC"],
]

# 動態發現的交易對，預設用 USDC 做 quote，交易量 1 SOL 等值
DYNAMIC_PAIR_AMOUNT = 1.0       # 動態發現交易對的預設掃描金額
DYNAMIC_PAIR_QUOTE  = "USDC"    # 動態發現時與哪個 quote token 比較
