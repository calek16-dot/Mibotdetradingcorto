"""
bot.py
======
Main entry point for the Binance Spot Testnet trading bot.

Run with:
    python -m trading_bot.bot

The bot wakes up at the close of each H1 candle, scans for Bullish
Order Blocks on SOL/USDT, XRP/USDT and TRX/USDT, places limit buy
orders at the OB midpoint, and manages open positions according to the
defined risk rules.

Scheduler logic
---------------
* On startup the bot computes the number of seconds until the next
  complete H1 candle close (xx:00:00 UTC) and sleeps until then.
* After the first cycle it repeats every 3600 seconds.
* A small SLEEP_BUFFER_SECONDS delay is added after the candle close
  to ensure the exchange has finalised the kline data.
"""

from __future__ import annotations

import math
import os
import signal
import sys
import time
from datetime import datetime, timezone

# Load .env file if present (optional convenience for local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; rely on shell environment

from trading_bot import config
from trading_bot.exchange import ExchangeClient
from trading_bot.logger import get_logger
from trading_bot.order_blocks import get_best_order_block
from trading_bot.risk_manager import PositionStore, RiskManager

log = get_logger(__name__)

# Extra seconds to wait after candle close before fetching klines
SLEEP_BUFFER_SECONDS: int = 5

# -------------------------------------------------------------------------
# Graceful shutdown
# -------------------------------------------------------------------------
_running = True


def _handle_signal(signum, frame):  # noqa: ANN001
    global _running
    log.info("Shutdown signal received (%s). Stopping after current cycle.", signum)
    _running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# -------------------------------------------------------------------------
# Scheduler helpers
# -------------------------------------------------------------------------

def _seconds_to_next_hour() -> float:
    """Return seconds remaining until the next full hour in UTC."""
    now = datetime.now(tz=timezone.utc)
    elapsed_seconds = now.minute * 60 + now.second + now.microsecond / 1e6
    return 3600.0 - elapsed_seconds


def _sleep_until_next_candle() -> None:
    """Sleep until the next H1 candle closes (with a small buffer)."""
    wait = _seconds_to_next_hour() + SLEEP_BUFFER_SECONDS
    wake_time = datetime.now(tz=timezone.utc)
    log.info(
        "Next cycle in %.0f s  (≈ %d min %d s)",
        wait, int(wait // 60), int(wait % 60),
    )
    # Sleep in chunks so we can react to shutdown signals
    deadline = time.monotonic() + wait
    while _running and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        time.sleep(min(remaining, 5.0))


# -------------------------------------------------------------------------
# Core cycle
# -------------------------------------------------------------------------

def _process_symbol(
    symbol: str,
    exchange: ExchangeClient,
    risk_manager: RiskManager,
) -> None:
    """
    Execute one analysis + management cycle for a single *symbol*.

    Steps
    -----
    1. Fetch the latest closed H1 candles.
    2. Check and manage any existing open position (SL / TP logic).
    3. If no open position, look for a Bullish Order Block and place a
       limit buy order at the OB midpoint if one is found.
    """
    log.info("--- Processing %s ---", symbol)

    # ---- 1. Fetch candles ----
    try:
        df = exchange.get_closed_candles(symbol)
    except Exception as exc:
        log.error("Failed to fetch candles for %s: %s", symbol, exc)
        return

    current_price = exchange.get_current_price(symbol)
    log.info("%s current price: %.6f", symbol, current_price)

    # ---- 2. Risk management on existing position ----
    risk_manager.evaluate(symbol, current_price)

    # ---- 3. Look for new entry if no open position ----
    if risk_manager.has_open_position(symbol):
        log.info("%s has an open position — skipping new entry scan.", symbol)
        return

    ob = get_best_order_block(df)
    if ob is None:
        log.info("%s — no valid Bullish Order Block found, no order placed.", symbol)
        return

    # ---- 4. Price must still be above the OB to be worth entering ----
    if current_price <= ob.midpoint:
        log.info(
            "%s — current price %.6f is at or below OB midpoint %.6f. "
            "Waiting for a retrace.",
            symbol, current_price, ob.midpoint,
        )
        return

    # ---- 5. Calculate position size (fraction of free USDT) ----
    free_usdt = exchange.get_free_balance("USDT")
    position_usdt = free_usdt * config.POSITION_SIZE_FRACTION
    quantity = position_usdt / ob.midpoint

    if quantity <= 0:
        log.warning("%s — calculated quantity %.8f is zero or negative.", symbol, quantity)
        return

    log.info(
        "%s — OB midpoint=%.6f | sl=%.6f | position_usdt=%.2f | qty=%.6f",
        symbol, ob.midpoint, ob.stop_loss, position_usdt, quantity,
    )

    # ---- 6. Place the limit buy ----
    order = exchange.place_limit_buy(symbol, quantity, ob.midpoint)
    if order is None:
        log.error("%s — order placement failed.", symbol)
        return

    order_id = int(order["orderId"])
    entry_price = float(order["price"])

    risk_manager.register_entry(
        symbol=symbol,
        order_id=order_id,
        entry_price=entry_price,
        quantity=exchange.round_quantity(symbol, quantity),
        stop_loss=ob.stop_loss,
    )


# -------------------------------------------------------------------------
# Main loop
# -------------------------------------------------------------------------

def run() -> None:
    """Initialise components and start the main scheduling loop."""
    log.info("=" * 60)
    log.info("Binance Spot Testnet Trading Bot — Starting up")
    log.info("Symbols : %s", ", ".join(config.SYMBOLS))
    log.info("Interval: %s", config.KLINE_INTERVAL)
    log.info("=" * 60)

    try:
        exchange = ExchangeClient()
    except EnvironmentError as exc:
        log.critical(str(exc))
        sys.exit(1)

    store = PositionStore()
    risk_manager = RiskManager(exchange, store)

    # Run an immediate cycle on startup so the bot is active right away,
    # then synchronise to the hourly schedule.
    first_run = True

    while _running:
        if not first_run:
            _sleep_until_next_candle()

        if not _running:
            break

        cycle_time = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log.info("====== Candle close cycle: %s ======", cycle_time)

        for symbol in config.SYMBOLS:
            if not _running:
                break
            try:
                _process_symbol(symbol, exchange, risk_manager)
            except Exception as exc:
                log.exception("Unexpected error processing %s: %s", symbol, exc)

        first_run = False

    log.info("Bot stopped cleanly.")


if __name__ == "__main__":
    run()
