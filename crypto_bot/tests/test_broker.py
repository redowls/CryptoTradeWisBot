"""Phase 9 (DB-free): order-request building + client-order-id idempotency."""

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from crypto_bot.execution import broker


def test_build_market_order_request():
    req = broker.build_order_request(symbol="BTC/USD", qty=0.5, side=OrderSide.BUY)
    assert isinstance(req, MarketOrderRequest)
    assert req.symbol == "BTC/USD"
    assert float(req.qty) == 0.5
    assert req.side == OrderSide.BUY
    assert req.time_in_force == TimeInForce.GTC


def test_build_limit_order_request():
    req = broker.build_order_request(symbol="ETH/USD", qty=2, side=OrderSide.SELL,
                                     limit_price=2500.0, client_order_id="ctwb-sig7")
    assert isinstance(req, LimitOrderRequest)
    assert float(req.limit_price) == 2500.0
    assert req.side == OrderSide.SELL
    assert req.client_order_id == "ctwb-sig7"


def test_client_order_id_deterministic_for_signal():
    # Same signal id -> same client order id (lets Alpaca reject duplicates).
    assert broker._client_order_id(42) == broker._client_order_id(42)
    assert broker._client_order_id(42) == "ctwb-sig42"
    assert broker._client_order_id(1) != broker._client_order_id(2)


def test_client_order_id_unique_when_no_signal():
    a = broker._client_order_id(None)
    b = broker._client_order_id(None)
    assert a != b
    assert a.startswith("ctwb-")
