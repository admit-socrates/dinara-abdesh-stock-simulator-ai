"""
AI Coach: the fallback path (no key) must be safe, and the live path (mocked
Claude) must be grounded, quota-limited, and never call the network in tests.
"""

import ai
import app as app_module
from conftest import register


def test_coach_page_renders_in_fallback(client):
    """With no API key the page still works and is honestly labelled rule-based."""
    register(client)
    page = client.get("/ai-coach").data
    assert page  # renders
    assert b"rule-based" in page
    assert b"chatForm" not in page  # no chat UI without a live model


def test_chat_endpoint_disabled_without_key(client):
    register(client)
    r = client.post("/api/ai/chat", json={"message": "hi"})
    assert r.status_code == 503
    assert "not switched on" in r.get_json()["error"]


def test_chat_answers_when_live(client, monkeypatch):
    """Live path: mock Claude so no key/network is needed."""
    monkeypatch.setattr(ai, "ai_available", lambda: True)
    monkeypatch.setattr(ai, "model_name", lambda: "claude-test")
    captured = {}

    def fake_answer(question, portfolio, holdings, movers):
        captured["question"] = question
        captured["portfolio"] = portfolio
        return "Here is a grounded, student-friendly answer.", None

    monkeypatch.setattr(ai, "coach_answer", fake_answer)

    register(client)
    client.post("/api/buy", json={"ticker": "AAPL", "shares": 10})
    r = client.post("/api/ai/chat", json={"message": "How am I doing?"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["answer"].startswith("Here is a grounded")
    # The endpoint really passed the user's live portfolio to the model.
    assert captured["question"] == "How am I doing?"
    assert captured["portfolio"]["cash"] == 9_000.0


def test_chat_empty_message_rejected(client, monkeypatch):
    monkeypatch.setattr(ai, "ai_available", lambda: True)
    register(client)
    r = client.post("/api/ai/chat", json={"message": "   "})
    assert r.status_code == 400


def test_daily_quota_enforced(client, monkeypatch):
    monkeypatch.setattr(ai, "ai_available", lambda: True)
    monkeypatch.setattr(ai, "coach_answer", lambda *a, **k: ("ok", None))
    monkeypatch.setattr(app_module, "AI_DAILY_LIMIT", 2)

    register(client)
    assert client.post("/api/ai/chat", json={"message": "q1"}).status_code == 200
    assert client.post("/api/ai/chat", json={"message": "q2"}).status_code == 200
    # Third call is over the limit.
    r = client.post("/api/ai/chat", json={"message": "q3"})
    assert r.status_code == 429
    assert "limit" in r.get_json()["error"].lower()


def test_explain_stock_fallback_is_unchanged(client, monkeypatch):
    """explain_stock returns the fallback dict verbatim when AI is unavailable."""
    monkeypatch.setattr(ai, "ai_available", lambda: False)
    fallback = {"why": "rule why", "student_takeaway": "rule takeaway", "sector": "x"}
    out = ai.explain_stock({"ticker": "AAPL"}, None, fallback=fallback)
    assert out["why"] == "rule why"
    assert out["ai_source"] == "rules"
