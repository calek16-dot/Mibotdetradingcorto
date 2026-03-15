"""
risk_manager.py
===============
Handles all risk management logic for open positions.

Rules
-----
1. When price rises +TP1_PCT % from entry  → sell 50 % of held qty,
   move stop-loss to entry price (breakeven).
2. When price rises +TP2_PCT % from entry  → sell remaining 100 % of
   held qty (close the position completely).
3. If price falls to or below stop_loss    → sell 100 % (emergency exit).

Position State Machine
----------------------
    WAITING_FILL  — limit buy order has been placed; not yet filled
    OPEN_FULL     — fully filled; no partial TP hit yet
    OPEN_HALF     — TP1 hit; 50 % sold, SL moved to breakeven
    CLOSED        — position fully closed
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from trading_bot import config
from trading_bot.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class PositionState(str, Enum):
    WAITING_FILL = "WAITING_FILL"
    OPEN_FULL    = "OPEN_FULL"
    OPEN_HALF    = "OPEN_HALF"
    CLOSED       = "CLOSED"


# ---------------------------------------------------------------------------
# Position dataclass
# ---------------------------------------------------------------------------

@dataclass
class Position:
    symbol: str
    order_id: int           # Binance order-ID of the entry limit order
    entry_price: float      # Filled price (set once order is FILLED)
    initial_qty: float      # Total quantity purchased
    remaining_qty: float    # Quantity still held
    stop_loss: float        # Current stop-loss price
    tp1_price: float        # Price for first partial take-profit
    tp2_price: float        # Price for full close
    state: PositionState = PositionState.WAITING_FILL
    tp1_hit: bool = False
    tp2_hit: bool = False
    opened_at: float = field(default_factory=time.time)  # Unix timestamp

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_order_block(
        cls,
        symbol: str,
        order_id: int,
        entry_price: float,
        quantity: float,
        stop_loss: float,
    ) -> "Position":
        tp1 = entry_price * (1.0 + config.TP1_PCT / 100.0)
        tp2 = entry_price * (1.0 + config.TP2_PCT / 100.0)
        return cls(
            symbol=symbol,
            order_id=order_id,
            entry_price=entry_price,
            initial_qty=quantity,
            remaining_qty=quantity,
            stop_loss=stop_loss,
            tp1_price=tp1,
            tp2_price=tp2,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        d = dict(d)
        d["state"] = PositionState(d["state"])
        return cls(**d)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

class PositionStore:
    """
    Persists positions to a JSON file so the bot can survive restarts
    without losing track of open trades.
    """

    def __init__(self, path: str = config.STATE_FILE) -> None:
        self._path = Path(path)
        self._positions: dict[str, Position] = {}
        self._load()

    # key = symbol
    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for sym, raw in data.items():
                    self._positions[sym] = Position.from_dict(raw)
                log.info("Loaded %d position(s) from %s", len(self._positions), self._path)
            except Exception as exc:
                log.error("Could not load state file %s: %s", self._path, exc)

    def _save(self) -> None:
        data = {sym: pos.to_dict() for sym, pos in self._positions.items()}
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def set(self, pos: Position) -> None:
        self._positions[pos.symbol] = pos
        self._save()

    def remove(self, symbol: str) -> None:
        self._positions.pop(symbol, None)
        self._save()

    def all(self) -> list[Position]:
        return list(self._positions.values())


# ---------------------------------------------------------------------------
# Risk Manager
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Evaluates open positions on every candle close and emits trade actions.

    Usage
    -----
        rm = RiskManager(exchange_client, position_store)
        rm.evaluate(symbol, current_price)
    """

    def __init__(self, exchange, store: PositionStore) -> None:
        self._ex = exchange
        self._store = store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_entry(
        self,
        symbol: str,
        order_id: int,
        entry_price: float,
        quantity: float,
        stop_loss: float,
    ) -> None:
        """
        Register a newly placed limit buy order.
        The position starts in WAITING_FILL state.
        """
        pos = Position.from_order_block(
            symbol=symbol,
            order_id=order_id,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
        )
        self._store.set(pos)
        log.info(
            "Position registered | %s | entry=%.6f | qty=%.6f | sl=%.6f | "
            "tp1=%.6f | tp2=%.6f",
            symbol, entry_price, quantity, stop_loss, pos.tp1_price, pos.tp2_price,
        )

    def evaluate(self, symbol: str, current_price: float) -> None:
        """
        Called on every candle close.  Checks the position state for
        *symbol* and executes necessary sells.
        """
        pos = self._store.get(symbol)
        if pos is None:
            return

        # ---- Step 1: Check fill status ----
        if pos.state == PositionState.WAITING_FILL:
            self._check_fill(pos)
            if pos.state == PositionState.WAITING_FILL:
                # Still not filled — check if the order is too stale
                log.debug("%s limit order %s not yet filled.", symbol, pos.order_id)
                return

        # ---- Step 2: Check stop-loss ----
        if current_price <= pos.stop_loss:
            log.warning(
                "STOP LOSS HIT | %s | price=%.6f <= sl=%.6f",
                symbol, current_price, pos.stop_loss,
            )
            self._close_position(pos, reason="stop-loss")
            return

        # ---- Step 3: Check TP2 (full close) ----
        if not pos.tp2_hit and current_price >= pos.tp2_price:
            log.info(
                "TP2 HIT | %s | price=%.6f >= tp2=%.6f",
                symbol, current_price, pos.tp2_price,
            )
            self._take_profit_2(pos)
            return

        # ---- Step 4: Check TP1 (partial close + breakeven SL) ----
        if not pos.tp1_hit and current_price >= pos.tp1_price:
            log.info(
                "TP1 HIT | %s | price=%.6f >= tp1=%.6f",
                symbol, current_price, pos.tp1_price,
            )
            self._take_profit_1(pos)
            return

        log.debug(
            "Position HOLD | %s | price=%.6f | sl=%.6f | tp1=%.6f | tp2=%.6f | "
            "state=%s | remaining=%.8f",
            symbol, current_price, pos.stop_loss,
            pos.tp1_price, pos.tp2_price, pos.state.value, pos.remaining_qty,
        )

    def has_open_position(self, symbol: str) -> bool:
        pos = self._store.get(symbol)
        return pos is not None and pos.state != PositionState.CLOSED

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_fill(self, pos: Position) -> None:
        """Poll order status; if FILLED, transition to OPEN_FULL."""
        status = self._ex.get_order_status(pos.symbol, pos.order_id)
        if status == "FILLED":
            filled_qty = self._ex.get_filled_qty(pos.symbol, pos.order_id)
            pos.state = PositionState.OPEN_FULL
            pos.initial_qty = filled_qty
            pos.remaining_qty = filled_qty
            # Recalculate TP levels from actual filled price
            pos.tp1_price = pos.entry_price * (1.0 + config.TP1_PCT / 100.0)
            pos.tp2_price = pos.entry_price * (1.0 + config.TP2_PCT / 100.0)
            self._store.set(pos)
            log.info(
                "Order FILLED | %s | qty=%.8f | entry=%.6f",
                pos.symbol, filled_qty, pos.entry_price,
            )
        elif status in ("CANCELED", "EXPIRED", "REJECTED"):
            log.warning(
                "Order %s for %s is %s. Removing position.",
                pos.order_id, pos.symbol, status,
            )
            self._store.remove(pos.symbol)

    def _take_profit_1(self, pos: Position) -> None:
        """Sell 50 % of remaining quantity; move SL to breakeven."""
        sell_qty = pos.remaining_qty * 0.5
        order = self._ex.place_market_sell(pos.symbol, sell_qty)
        if order is not None:
            pos.remaining_qty -= sell_qty
            pos.tp1_hit = True
            pos.stop_loss = pos.entry_price   # Breakeven
            pos.state = PositionState.OPEN_HALF
            self._store.set(pos)
            log.info(
                "TP1 executed | %s | sold=%.8f | new_sl=%.6f (breakeven) | "
                "remaining=%.8f",
                pos.symbol, sell_qty, pos.stop_loss, pos.remaining_qty,
            )

    def _take_profit_2(self, pos: Position) -> None:
        """Sell 100 % of remaining quantity."""
        order = self._ex.place_market_sell(pos.symbol, pos.remaining_qty)
        if order is not None:
            pos.tp2_hit = True
            pos.state = PositionState.CLOSED
            pos.remaining_qty = 0.0
            self._store.set(pos)
            log.info("TP2 executed | %s | position CLOSED.", pos.symbol)
            self._store.remove(pos.symbol)

    def _close_position(self, pos: Position, reason: str = "unknown") -> None:
        """Emergency close: sell all remaining qty at market."""
        order = self._ex.place_market_sell(pos.symbol, pos.remaining_qty)
        if order is not None:
            pos.state = PositionState.CLOSED
            pos.remaining_qty = 0.0
            self._store.set(pos)
            log.info("Position CLOSED | %s | reason=%s", pos.symbol, reason)
            self._store.remove(pos.symbol)
