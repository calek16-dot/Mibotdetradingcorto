"""
exchange.py
===========
Thin wrapper around the ``python-binance`` client that:

* Reads credentials from environment variables
* Forces Spot Testnet mode
* Provides helper methods used by the rest of the bot
* Handles Binance API errors gracefully and logs them
"""

from __future__ import annotations

import math
import os
from decimal import ROUND_DOWN, Decimal
from typing import Any

import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException

from trading_bot import config
from trading_bot.logger import get_logger

log = get_logger(__name__)


def _load_client() -> Client:
    """Instantiate a Binance Spot Testnet client using env-vars for keys."""
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_SECRET_KEY", "")

    if not api_key or not api_secret:
        raise EnvironmentError(
            "Environment variables BINANCE_API_KEY and BINANCE_SECRET_KEY "
            "must be set before starting the bot."
        )

    client = Client(api_key, api_secret, testnet=True)
    log.info("Connected to Binance Spot Testnet.")
    return client


class ExchangeClient:
    """Singleton-like wrapper that exposes all exchange interactions."""

    def __init__(self) -> None:
        self._client = _load_client()
        self._symbol_info_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_closed_candles(self, symbol: str, limit: int | None = None) -> pd.DataFrame:
        """
        Fetch closed H1 candles for *symbol* and return them as a DataFrame.

        The most-recent candle (index -1) is the **currently open** candle
        and is excluded — the bot should only act on closed candles.

        Columns: open_time, open, high, low, close, volume
        """
        n = (limit or config.CANDLES_TO_FETCH) + 1  # +1 to discard the live candle
        raw = self._client.get_klines(
            symbol=symbol,
            interval=config.KLINE_INTERVAL,
            limit=n,
        )
        df = pd.DataFrame(
            raw,
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_asset_volume", "num_trades",
                "taker_buy_base_vol", "taker_buy_quote_vol", "ignore",
            ],
        )
        # Keep only what we need; cast to float
        df = df[["open_time", "open", "high", "low", "close", "volume"]].copy()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)

        # Drop the still-open last candle
        df = df.iloc[:-1].reset_index(drop=True)
        log.debug("Fetched %d closed candles for %s", len(df), symbol)
        return df

    def get_current_price(self, symbol: str) -> float:
        """Return the latest mark price from the order book ticker."""
        ticker = self._client.get_symbol_ticker(symbol=symbol)
        return float(ticker["price"])

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_free_balance(self, asset: str) -> float:
        """Return the free balance for *asset* (e.g. 'USDT', 'SOL')."""
        account = self._client.get_account()
        for b in account["balances"]:
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    # ------------------------------------------------------------------
    # Symbol filters
    # ------------------------------------------------------------------

    def _get_symbol_info(self, symbol: str) -> dict:
        if symbol not in self._symbol_info_cache:
            info = self._client.get_symbol_info(symbol)
            if info is None:
                raise ValueError(f"Symbol {symbol} not found on exchange.")
            self._symbol_info_cache[symbol] = info
        return self._symbol_info_cache[symbol]

    def _get_lot_size_filter(self, symbol: str) -> dict:
        info = self._get_symbol_info(symbol)
        for f in info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                return f
        raise ValueError(f"LOT_SIZE filter not found for {symbol}")

    def _get_price_filter(self, symbol: str) -> dict:
        info = self._get_symbol_info(symbol)
        for f in info["filters"]:
            if f["filterType"] == "PRICE_FILTER":
                return f
        raise ValueError(f"PRICE_FILTER not found for {symbol}")

    def round_quantity(self, symbol: str, qty: float) -> float:
        """Round *qty* down to the allowed step size for *symbol*."""
        lot = self._get_lot_size_filter(symbol)
        step = Decimal(lot["stepSize"])
        result = Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN)
        return float(result)

    def round_price(self, symbol: str, price: float) -> float:
        """Round *price* to the allowed tick size for *symbol*."""
        pf = self._get_price_filter(symbol)
        tick = Decimal(pf["tickSize"])
        result = Decimal(str(price)).quantize(tick, rounding=ROUND_DOWN)
        return float(result)

    def get_min_qty(self, symbol: str) -> float:
        lot = self._get_lot_size_filter(symbol)
        return float(lot["minQty"])

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_limit_buy(
        self, symbol: str, quantity: float, price: float
    ) -> dict[str, Any] | None:
        """
        Place a GTC limit buy order.

        Returns the Binance order response dict or None on failure.
        """
        qty = self.round_quantity(symbol, quantity)
        px = self.round_price(symbol, price)

        if qty < self.get_min_qty(symbol):
            log.warning(
                "Quantity %.8f for %s is below minimum. Skipping order.", qty, symbol
            )
            return None

        log.info("Placing LIMIT BUY | %s | qty=%.8f | price=%.8f", symbol, qty, px)
        try:
            order = self._client.create_order(
                symbol=symbol,
                side=Client.SIDE_BUY,
                type=Client.ORDER_TYPE_LIMIT,
                timeInForce=config.TIME_IN_FORCE,
                quantity=str(qty),
                price=str(px),
            )
            log.info("Order placed: id=%s status=%s", order["orderId"], order["status"])
            return order
        except (BinanceAPIException, BinanceOrderException) as exc:
            log.error("Failed to place LIMIT BUY for %s: %s", symbol, exc)
            return None

    def place_market_sell(
        self, symbol: str, quantity: float
    ) -> dict[str, Any] | None:
        """
        Place a market sell order for *quantity* units of *symbol*.

        Returns the Binance order response dict or None on failure.
        """
        # Derive the base asset from the symbol name
        base_asset = self._get_symbol_info(symbol)["baseAsset"]
        qty = self.round_quantity(symbol, quantity)

        if qty < self.get_min_qty(symbol):
            log.warning(
                "Sell quantity %.8f for %s is below minimum. Skipping.", qty, symbol
            )
            return None

        log.info("Placing MARKET SELL | %s | qty=%.8f", symbol, qty)
        try:
            order = self._client.create_order(
                symbol=symbol,
                side=Client.SIDE_SELL,
                type=Client.ORDER_TYPE_MARKET,
                quantity=str(qty),
            )
            log.info(
                "Market sell executed: id=%s status=%s",
                order["orderId"],
                order["status"],
            )
            return order
        except (BinanceAPIException, BinanceOrderException) as exc:
            log.error("Failed to place MARKET SELL for %s: %s", symbol, exc)
            return None

    def cancel_open_orders(self, symbol: str) -> None:
        """Cancel all open orders for *symbol*."""
        try:
            open_orders = self._client.get_open_orders(symbol=symbol)
            for o in open_orders:
                self._client.cancel_order(symbol=symbol, orderId=o["orderId"])
                log.info("Cancelled order id=%s for %s", o["orderId"], symbol)
        except BinanceAPIException as exc:
            log.error("Error cancelling orders for %s: %s", symbol, exc)

    def get_order_status(self, symbol: str, order_id: int) -> str | None:
        """Return the status string for a known order, or None on error."""
        try:
            order = self._client.get_order(symbol=symbol, orderId=order_id)
            return order["status"]
        except BinanceAPIException as exc:
            log.error("Could not fetch order %s for %s: %s", order_id, symbol, exc)
            return None

    def get_filled_qty(self, symbol: str, order_id: int) -> float:
        """Return executedQty for a known order."""
        try:
            order = self._client.get_order(symbol=symbol, orderId=order_id)
            return float(order.get("executedQty", 0.0))
        except BinanceAPIException as exc:
            log.error("Could not fetch order %s for %s: %s", order_id, symbol, exc)
            return 0.0
