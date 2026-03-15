"""
order_blocks.py
===============
Detects **Bullish Order Blocks (OB)** on a DataFrame of OHLCV candles.

Definition used
---------------
A Bullish Order Block is the last *bearish* candle (close < open) that
immediately precedes a strong bullish impulse move.  An "impulse" is
confirmed when the subsequent candles move upward by at least
``OB_IMPULSE_MULTIPLIER × body_size`` of the bearish candle.

The entry price is placed at the *midpoint* of the OB candle:
    entry = (ob_high + ob_low) / 2

The stop-loss is set ``INITIAL_SL_PCT`` percent below the OB low:
    stop_loss = ob_low * (1 - INITIAL_SL_PCT / 100)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from trading_bot import config
from trading_bot.logger import get_logger

log = get_logger(__name__)


@dataclass
class OrderBlock:
    """A detected Bullish Order Block."""

    index: int               # DataFrame row index of the OB candle
    open_time: pd.Timestamp  # Candle open timestamp
    ob_high: float           # High of the OB candle
    ob_low: float            # Low of the OB candle
    ob_open: float           # Open of the OB candle
    ob_close: float          # Close of the OB candle
    midpoint: float          # Entry price (50% of OB candle)
    stop_loss: float         # Initial SL price
    age_candles: int         # How many candles ago the OB formed


def detect_bullish_order_blocks(df: pd.DataFrame) -> list[OrderBlock]:
    """
    Scan *df* (oldest-first) for Bullish Order Blocks.

    Parameters
    ----------
    df : pd.DataFrame
        Columns: open_time, open, high, low, close, volume
        Must contain at least ``OB_LOOKBACK + 2`` rows.

    Returns
    -------
    list[OrderBlock]
        All valid OBs found, sorted from most-recent to oldest.
        OBs older than ``OB_MAX_AGE_CANDLES`` are excluded.
    """
    if len(df) < config.OB_LOOKBACK + 2:
        log.debug("Not enough candles to detect OBs (%d < %d)", len(df), config.OB_LOOKBACK + 2)
        return []

    results: list[OrderBlock] = []
    last_idx = len(df) - 1

    # Iterate over candles that could be the OB (leave room for impulse window)
    for i in range(len(df) - config.OB_LOOKBACK - 1):
        candle = df.iloc[i]

        # ---- Condition 1: The OB candle must be bearish ----
        is_bearish = candle["close"] < candle["open"]
        if not is_bearish:
            continue

        body_size = candle["open"] - candle["close"]
        if body_size <= 0:
            continue  # Doji guard

        # ---- Condition 2: Impulse confirmation ----
        # Look at the next OB_LOOKBACK candles.
        # The highest close in that window must be above the OB high by at
        # least OB_IMPULSE_MULTIPLIER × body_size (strong bullish rejection).
        impulse_window = df.iloc[i + 1 : i + 1 + config.OB_LOOKBACK]
        impulse_high = impulse_window["high"].max()
        required_move = candle["high"] + body_size * config.OB_IMPULSE_MULTIPLIER

        if impulse_high < required_move:
            continue  # Not a strong enough impulse

        # ---- Condition 3: OB must not be too old ----
        age = last_idx - i
        if age > config.OB_MAX_AGE_CANDLES:
            continue

        # ---- Condition 4: Price must still be above the OB (unmitigated) ----
        # If price has already traded down into the OB area (closing below OB
        # midpoint) the block is considered "mitigated" and ignored.
        subsequent = df.iloc[i + 1 :]
        midpoint = (candle["high"] + candle["low"]) / 2.0
        if (subsequent["low"] < midpoint).any():
            log.debug(
                "OB at index %d mitigated — price traded through midpoint %.6f",
                i, midpoint,
            )
            continue

        stop_loss = candle["low"] * (1.0 - config.INITIAL_SL_PCT / 100.0)

        ob = OrderBlock(
            index=i,
            open_time=candle["open_time"],
            ob_high=candle["high"],
            ob_low=candle["low"],
            ob_open=candle["open"],
            ob_close=candle["close"],
            midpoint=midpoint,
            stop_loss=stop_loss,
            age_candles=age,
        )
        results.append(ob)
        log.debug(
            "OB detected | time=%s | mid=%.6f | sl=%.6f | age=%d candles",
            ob.open_time, ob.midpoint, ob.stop_loss, ob.age_candles,
        )

    # Sort: most recent first
    results.sort(key=lambda o: o.index, reverse=True)
    return results


def get_best_order_block(df: pd.DataFrame) -> Optional[OrderBlock]:
    """
    Return the single most-recent, unmitigated Bullish OB from *df*, or
    None if no valid block exists.
    """
    blocks = detect_bullish_order_blocks(df)
    if blocks:
        best = blocks[0]
        log.info(
            "Best OB | time=%s | mid=%.6f | sl=%.6f | age=%d candles",
            best.open_time, best.midpoint, best.stop_loss, best.age_candles,
        )
        return best
    log.info("No valid Bullish Order Block found.")
    return None
