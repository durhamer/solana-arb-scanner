"""
Whale Tracker — main entry point.

Usage:
    python main.py discover                  # discover smart-money wallets
    python main.py monitor                   # monitor tracked wallets in real time (TODO)
    python main.py analyze                   # analyze copy-trade performance (TODO)
    python main.py lookup <token_mint>       # find smart wallets holding a token
"""

import asyncio
import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "discover"

    if command == "discover":
        from wallet_discovery import run_discovery
        asyncio.run(run_discovery())
    elif command == "monitor":
        from monitor import run_monitor
        run_monitor()
    elif command == "analyze":
        from analyzer import run_analysis
        run_analysis()
    elif command == "lookup":
        if len(sys.argv) < 3:
            print("Usage: python main.py lookup <token_mint_address>")
            sys.exit(1)
        from lookup import run_lookup
        run_lookup(sys.argv[2])
    else:
        print(f"Unknown command: {command}")
        print("Usage: python main.py [discover|monitor|analyze|lookup <token_mint>]")
        sys.exit(1)


if __name__ == "__main__":
    main()
