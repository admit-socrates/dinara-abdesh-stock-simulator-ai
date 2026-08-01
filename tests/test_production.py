"""Production-hardening: history page, health check, input caps, headers, AI memory."""

import ai
from conftest import register


def test_healthz_ok(client):
    body = client.get("/healthz").get_json()
    assert body["status"] == "ok"
    assert body["prices_cached"] == 4  # the seeded rows
    assert "ai_coach" in body


def test_security_headers_present(client):
    r = client.get("/login")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"


def test_history_empty_then_records(client):
    register(client)
    assert b"No trades yet" in client.get("/history").data
    client.post("/api/buy", json={"ticker": "AAPL", "shares": 2})
    page = client.get("/history").data
    assert b"AAPL" in page and b"BUY" in page


def test_oversized_order_rejected(client):
    register(client)
    r = client.post("/api/buy", json={"ticker": "AAPL", "shares": 2_000_000})
    assert r.status_code == 400  # exceeds MAX_SHARES


def test_non_numeric_shares_rejected(client):
    register(client)
    r = client.post("/api/buy", json={"ticker": "AAPL", "shares": "lots"})
    assert r.status_code == 400


def test_atomic_buy_leaves_cash_intact_on_failure(client):
    register(client)
    # Cost just over balance -> conditional UPDATE must not touch cash.
    client.post("/api/buy", json={"ticker": "NVDA", "shares": 24})  # 24*400=9600 ok
    r = client.post("/api/buy", json={"ticker": "NVDA", "shares": 2})  # +800 > remaining 400
    assert r.status_code == 400
    assert client.get("/api/cash").get_json()["cash"] == 400.0


def test_ai_chat_remembers_history(client, monkeypatch):
    monkeypatch.setattr(ai, "ai_available", lambda: True)
    seen = []

    def fake_answer(question, portfolio, holdings, movers, history=None):
        seen.append(list(history or []))
        return f"answer to: {question}", None

    monkeypatch.setattr(ai, "coach_answer", fake_answer)
    register(client)

    client.post("/api/ai/chat", json={"message": "first question"})
    client.post("/api/ai/chat", json={"message": "second question"})

    # First call got no history; second call saw the first exchange.
    assert seen[0] == []
    assert any(t["content"] == "first question" for t in seen[1])
    assert any(t["role"] == "assistant" for t in seen[1])
