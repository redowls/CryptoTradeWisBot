"""Order execution + position tracking (Phase 9) — PAPER endpoint by default.

Places crypto orders on Alpaca and records them to the `trades` table. Safety:
  - The bot trades on the PAPER endpoint unless `LIVE_TRADING_ENABLED` is true
    (default false) — `get_trading_client()` sets ``paper=not live_enabled()``.
  - One signal can't double-fire: `place_signal_order` skips a signal that
    already has a non-cancelled trade row, and uses a deterministic
    `client_order_id` so Alpaca itself also rejects duplicates.
  - `reconcile_open_positions()` re-syncs the DB with Alpaca on startup so the
    bot recovers cleanly from restarts.

Alpaca crypto does not support bracket/OCO orders, so the protective stop/target
are recorded on the trade row and enforced by the bot (exit management, Phase 10).

CLI:  python -m crypto_bot.execution.broker        # account + reconcile (no orders)
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from sqlalchemy import and_, select

from crypto_bot import config_store
from crypto_bot.db import get_session
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import Trade

log = get_logger(__name__)

_FILL_POLL_TRIES = 10
_FILL_POLL_SLEEP = 0.5


def live_enabled() -> bool:
    return config_store.get_bool("LIVE_TRADING_ENABLED", False)


def get_trading_client() -> TradingClient:
    """Alpaca trading client. PAPER unless LIVE_TRADING_ENABLED is true."""
    key = config_store.get("ALPACA_API_KEY")
    secret = config_store.get("ALPACA_SECRET")
    paper = not live_enabled()
    if not paper:
        log.warning("LIVE_TRADING_ENABLED is true — using the LIVE trading endpoint!")
    return TradingClient(key, secret, paper=paper)


def get_account(client: TradingClient | None = None) -> dict:
    client = client or get_trading_client()
    a = client.get_account()
    return {
        "account_number": a.account_number, "status": str(a.status),
        "equity": float(a.equity), "cash": float(a.cash),
        "buying_power": float(a.buying_power), "currency": a.currency,
    }


def get_positions(client: TradingClient | None = None) -> list[dict]:
    client = client or get_trading_client()
    out = []
    for p in client.get_all_positions():
        out.append({
            "symbol": p.symbol, "qty": float(p.qty),
            "avg_entry_price": float(p.avg_entry_price),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
        })
    return out


def _client_order_id(signal_id: int | None, tag: str = "ctwb") -> str:
    if signal_id is not None:
        return f"{tag}-sig{signal_id}"
    return f"{tag}-{uuid.uuid4().hex[:16]}"


def _await_fill(client: TradingClient, order_id: str):
    """Poll an order until it leaves a pending state; return the latest order."""
    order = client.get_order_by_id(order_id)
    for _ in range(_FILL_POLL_TRIES):
        status = str(order.status).lower()
        if any(s in status for s in ("filled", "rejected", "canceled", "cancelled", "expired")):
            break
        time.sleep(_FILL_POLL_SLEEP)
        order = client.get_order_by_id(order_id)
    return order


def build_order_request(*, symbol: str, qty: float, side: OrderSide,
                        limit_price: float | None = None, client_order_id: str | None = None):
    """Build a market or limit crypto order request (GTC). Pure — unit-testable."""
    common = dict(symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.GTC,
                  client_order_id=client_order_id)
    if limit_price is not None:
        return LimitOrderRequest(limit_price=limit_price, **common)
    return MarketOrderRequest(**common)


def submit_order(client: TradingClient, *, symbol: str, qty: float, side: OrderSide,
                 limit_price: float | None = None, client_order_id: str | None = None):
    """Submit a market or limit crypto order (GTC). Returns the Alpaca order."""
    return client.submit_order(build_order_request(
        symbol=symbol, qty=qty, side=side, limit_price=limit_price,
        client_order_id=client_order_id))


def record_trade(session, *, symbol: str, side: str, qty: float,
                 entry_price: float | None, stop: float | None, target: float | None,
                 alpaca_order_id: str, signal_id: int | None, score: float | None,
                 source: str | None, risk_pct: float | None, is_paper: bool,
                 status: str = "OPEN") -> Trade:
    row = Trade(
        signal_id=signal_id, symbol=symbol, side=side, qty=qty,
        entry_price=entry_price, entry_time=datetime.now(timezone.utc),
        stop_price=stop, target_price=target, status=status,
        confidence_score=score, signal_source=source, risk_pct=risk_pct,
        alpaca_order_id=alpaca_order_id, is_paper=is_paper,
    )
    session.add(row)
    session.commit()
    return row


def _existing_trade_for_signal(session, signal_id: int) -> Trade | None:
    return session.scalar(
        select(Trade).where(and_(Trade.signal_id == signal_id, Trade.status != "CANCELLED"))
    )


def place_order(session, *, symbol: str, qty: float, stop: float | None = None,
                target: float | None = None, signal_id: int | None = None,
                score: float | None = None, source: str | None = None,
                risk_pct: float | None = None, side: OrderSide = OrderSide.BUY,
                limit_price: float | None = None,
                client: TradingClient | None = None) -> dict:
    """Submit a (paper) order and record it to `trades`. Idempotent per signal."""
    client = client or get_trading_client()
    paper = not live_enabled()

    if signal_id is not None:
        dup = _existing_trade_for_signal(session, signal_id)
        if dup is not None:
            log.warning("Signal %s already has trade %s — skipping (dedupe).", signal_id, dup.trade_id)
            return {"skipped": True, "reason": "duplicate_signal", "trade_id": dup.trade_id}

    coid = _client_order_id(signal_id)
    order = submit_order(client, symbol=symbol, qty=qty, side=side,
                         limit_price=limit_price, client_order_id=coid)
    order = _await_fill(client, str(order.id))

    status = str(order.status).lower()
    filled_qty = float(order.filled_qty or 0)
    filled_price = float(order.filled_avg_price) if order.filled_avg_price else None
    filled = ("filled" in status) and filled_qty > 0

    if any(s in status for s in ("rejected", "canceled", "cancelled", "expired")):
        log.error("Order %s for %s %s — not recorded.", order.id, symbol, status)
        return {"filled": False, "recorded": False, "status": status,
                "alpaca_order_id": str(order.id)}

    # Record on acceptance. If the fill is still pending, entry_price stays NULL
    # and sync_fills() populates it once the order fills (orders fill async).
    trade = record_trade(
        session, symbol=symbol, side=side.value.upper(),
        qty=(filled_qty if filled else qty), entry_price=filled_price,
        stop=stop, target=target, alpaca_order_id=str(order.id),
        signal_id=signal_id, score=score, source=source, risk_pct=risk_pct,
        is_paper=paper, status="OPEN",
    )
    log.info("%s %s qty=%s @ %s -> trade #%d (filled=%s, paper=%s)",
             side.value.upper(), symbol, trade.qty, filled_price, trade.trade_id, filled, paper)
    return {"filled": filled, "recorded": True, "trade_id": trade.trade_id,
            "qty": float(trade.qty), "entry_price": filled_price,
            "status": status, "alpaca_order_id": str(order.id)}


def sync_fills(session, client: TradingClient | None = None) -> int:
    """Populate entry_price/qty for OPEN trades whose order has since filled."""
    client = client or get_trading_client()
    pending = session.scalars(
        select(Trade).where(and_(
            Trade.status == "OPEN", Trade.entry_price.is_(None),
            Trade.alpaca_order_id.isnot(None),
        ))
    ).all()
    updated = 0
    for t in pending:
        try:
            o = client.get_order_by_id(t.alpaca_order_id)
        except Exception as e:  # noqa: BLE001
            log.warning("sync_fills: order %s lookup failed: %s", t.alpaca_order_id, e)
            continue
        if "filled" in str(o.status).lower() and o.filled_avg_price:
            t.entry_price = float(o.filled_avg_price)
            t.qty = float(o.filled_qty or t.qty)
            updated += 1
    session.commit()
    return updated


def cancel_trade_order(session, trade_id: int, client: TradingClient | None = None) -> dict:
    """Cancel a trade's working order at Alpaca and mark the row CANCELLED."""
    client = client or get_trading_client()
    t = session.get(Trade, trade_id)
    if t is None:
        return {"cancelled": False, "reason": "no_such_trade"}
    if t.alpaca_order_id:
        try:
            client.cancel_order_by_id(t.alpaca_order_id)
        except Exception as e:  # noqa: BLE001 — already filled/cancelled is fine
            log.warning("cancel_trade_order: %s", e)
    t.status = "CANCELLED"
    session.commit()
    return {"cancelled": True, "trade_id": trade_id}


def close_position(session, symbol: str, client: TradingClient | None = None) -> dict:
    """Liquidate a symbol on Alpaca and close its open trade row(s)."""
    client = client or get_trading_client()
    order = client.close_position(symbol)
    order = _await_fill(client, str(order.id))
    fill = float(order.filled_avg_price) if order.filled_avg_price else None

    rows = session.scalars(
        select(Trade).where(and_(Trade.symbol == symbol, Trade.status == "OPEN"))
    ).all()
    for t in rows:
        t.exit_price = fill
        t.exit_time = datetime.now(timezone.utc)
        if t.entry_price is not None and fill is not None:
            t.realized_pnl = round(float(t.qty) * (fill - float(t.entry_price)), 8)
            t.realized_pnl_pct = round((fill / float(t.entry_price) - 1.0) * 100.0, 4)
        t.status = "CLOSED"
    session.commit()
    log.info("Closed %s @ %s (%d trade row(s)).", symbol, fill, len(rows))
    return {"symbol": symbol, "exit_price": fill, "closed_rows": len(rows)}


def reconcile_open_positions(session, client: TradingClient | None = None) -> dict:
    """Sync DB open trades with Alpaca positions (restart recovery)."""
    client = client or get_trading_client()
    live = {p["symbol"]: p for p in get_positions(client)}
    open_rows = session.scalars(select(Trade).where(Trade.status == "OPEN")).all()
    db_symbols = {t.symbol for t in open_rows}

    # A *filled* DB position (entry_price set) with no Alpaca position was exited
    # elsewhere -> mark closed. Pending rows (entry_price NULL) are left for
    # sync_fills(); their working order may still fill.
    orphaned = 0
    for t in open_rows:
        if t.entry_price is not None and t.symbol not in live:
            t.status = "CLOSED"
            t.exit_time = datetime.now(timezone.utc)
            orphaned += 1
    session.commit()
    untracked = [s for s in live if s not in db_symbols]
    if untracked:
        log.warning("Alpaca positions with no OPEN trade row: %s", untracked)
    return {"alpaca_positions": len(live), "db_open": len(open_rows),
            "closed_orphans": orphaned, "untracked": untracked}


if __name__ == "__main__":
    client = get_trading_client()
    acct = get_account(client)
    print(f"paper={not live_enabled()}  account={acct['account_number']} "
          f"status={acct['status']} equity={acct['equity']:.2f} cash={acct['cash']:.2f}")
    for p in get_positions(client):
        print(f"  position {p['symbol']} qty={p['qty']} @ {p['avg_entry_price']}")
    with get_session() as s:
        print("reconcile:", reconcile_open_positions(s, client))
