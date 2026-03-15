"""
config.py
=========
Central configuration for the Binance Spot Testnet trading bot.

All strategy parameters, risk rules and symbol settings live here.
Edit only this file to change bot behaviour — no other file needs
to be touched for standard tuning.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Symbols to trade
# ---------------------------------------------------------------------------
SYMBOLS: list[str] = [
    "SOLUSDT",
    "XRPUSDT",
    "TRXUSDT",
]

# ---------------------------------------------------------------------------
# Timeframe
# ---------------------------------------------------------------------------
KLINE_INTERVAL: str = "1h"          # Binance kline interval string
CANDLES_TO_FETCH: int = 100         # How many closed candles to analyse

# ---------------------------------------------------------------------------
# Order Block detection
# ---------------------------------------------------------------------------
# A Bullish Order Block is identified as the last *bearish* candle that
# immediately precedes a strong bullish impulse move.
#
# OB_IMPULSE_MULTIPLIER: the next N candles after the bearish candle must
# produce a total range (high-to-low of the move) that is at least this
# many times the bearish candle's body.
OB_LOOKBACK: int = 5                # Candles to look back for impulse confirmation
OB_IMPULSE_MULTIPLIER: float = 1.5  # Minimum impulse size relative to OB body
OB_MAX_AGE_CANDLES: int = 20        # Ignore OBs older than this many candles

# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
# Buy limit at the midpoint (50 %) of the Order Block candle.
# midpoint = (ob_high + ob_low) / 2
ORDER_TYPE: str = "LIMIT"
TIME_IN_FORCE: str = "GTC"

# Fractional position size expressed as a fraction of free USDT balance.
# 0.10 = use 10 % of available USDT per trade.
POSITION_SIZE_FRACTION: float = 0.10

# ---------------------------------------------------------------------------
# Risk Management
# ---------------------------------------------------------------------------
# Take-profit levels (percentage gain from entry)
TP1_PCT: float = 2.0   # Sell 50 % of position; move SL to breakeven
TP2_PCT: float = 4.0   # Sell remaining 100 %

# Initial stop-loss: distance below the Order Block low (percentage)
INITIAL_SL_PCT: float = 1.5   # 1.5 % below OB low

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
STATE_FILE: str = "bot_state.json"  # Tracks open positions between cycles

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE: str = "trading_bot.log"
LOG_LEVEL: str = "INFO"
