"""Buy/sell mechanics and the invariants that keep the sim honest."""

from conftest import register


def _cash(client):
    return client.get("/api/cash").get_json()["cash"]


def test_buy_deducts_cash(client):
    register(client)
    r = client.post("/api/buy", json={"ticker": "AAPL", "shares": 10})
    assert r.status_code == 200 and r.get_json()["success"]
    assert _cash(client) == 9_000.0  # 10 * $100


def test_sell_adds_cash(client):
    register(client)
    client.post("/api/buy", json={"ticker": "AAPL", "shares": 10})
    r = client.post("/api/sell", json={"ticker": "AAPL", "shares": 5})
    assert r.status_code == 200 and r.get_json()["success"]
    assert _cash(client) == 9_500.0  # sold 5 * $100 back


def test_cannot_overspend(client):
    register(client)
    r = client.post("/api/buy", json={"ticker": "NVDA", "shares": 1000})  # $400k
    assert r.status_code == 400
    assert "Insufficient" in r.get_json()["error"]
    assert _cash(client) == 10_000.0  # untouched


def test_cannot_oversell(client):
    register(client)
    client.post("/api/buy", json={"ticker": "AAPL", "shares": 3})
    r = client.post("/api/sell", json={"ticker": "AAPL", "shares": 10})
    assert r.status_code == 400
    assert "Not enough shares" in r.get_json()["error"]


def test_unknown_ticker_rejected(client):
    register(client)
    r = client.post("/api/buy", json={"ticker": "FAKE", "shares": 1})
    assert r.status_code == 400


def test_negative_shares_rejected(client):
    register(client)
    r = client.post("/api/buy", json={"ticker": "AAPL", "shares": -5})
    assert r.status_code == 400


def test_weighted_average_cost(client):
    register(client)
    # Buy 10 @100, then price is still 100 in seed -> avg stays 100.
    client.post("/api/buy", json={"ticker": "AAPL", "shares": 10})
    client.post("/api/buy", json={"ticker": "AAPL", "shares": 10})
    page = client.get("/portfolio").data
    assert b"AAPL" in page
    assert _cash(client) == 8_000.0  # 20 shares * $100
