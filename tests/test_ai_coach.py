"""
AI Coach: the fallback path (no key) must be safe, and the live path (mocked
Claude) must be grounded, quota-limited, and never call the network in tests.
"""

import json

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

    def fake_answer(question, portfolio, holdings, movers, history=None):
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


def test_provider_prefers_groq_over_gemini(monkeypatch):
    """When both keys are set, Groq wins; model_name reflects the active provider."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza_test")
    assert ai.provider_name() == "groq"
    assert ai.ai_available() is True
    assert ai.model_name() in ai.GROQ_MODELS


def test_provider_falls_back_to_gemini(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza_test")
    assert ai.provider_name() == "gemini"
    assert ai.model_name() in ai.GEMINI_MODELS


def test_no_provider_when_no_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert ai.provider_name() is None
    assert ai.ai_available() is False
    assert ai.model_name() is None


def test_groq_call_parses_openai_shape(monkeypatch):
    """_call_messages routes to Groq and parses its OpenAI-style response."""
    import io

    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "Grounded Groq answer."}}]}
            ).encode("utf-8")

    def fake_urlopen(req, timeout=20):
        # Confirms we hit Groq's endpoint with a bearer token, not Gemini.
        assert req.full_url == ai._GROQ_URL
        assert req.headers["Authorization"] == "Bearer gsk_test"
        return FakeResp()

    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    out = ai._call_messages([{"role": "user", "content": "hi"}])
    assert out == "Grounded Groq answer."


def test_explain_stock_fallback_is_unchanged(client, monkeypatch):
    """explain_stock returns the fallback dict verbatim when AI is unavailable."""
    monkeypatch.setattr(ai, "ai_available", lambda: False)
    fallback = {"why": "rule why", "student_takeaway": "rule takeaway", "sector": "x"}
    out = ai.explain_stock({"ticker": "AAPL"}, None, fallback=fallback)
    assert out["why"] == "rule why"
    assert out["ai_source"] == "rules"


def test_groq_request_sends_explicit_user_agent(monkeypatch):
    """Regression (2026-08-22): Groq sits behind Cloudflare, which rejected the
    stdlib default User-Agent ("Python-urllib/3.x") with 403 "error code: 1010"
    before the request reached the API. The coach answered "busy" for every
    question even with a valid key. Every provider call must send an explicit UA."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}
            ).encode("utf-8")

    def fake_urlopen(req, timeout=20):
        seen["ua"] = req.get_header("User-agent")
        return FakeResp()

    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    ai._call_messages([{"role": "user", "content": "hi"}])

    assert seen["ua"], "no User-Agent sent — Cloudflare will return 403/1010"
    assert "urllib" not in seen["ua"].lower()
    assert seen["ua"] == ai._USER_AGENT


def test_gemini_request_sends_explicit_user_agent(monkeypatch):
    """Same guard on the Gemini path, so a future edge rule cannot bite us there."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza_test")
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
            ).encode("utf-8")

    def fake_urlopen(req, timeout=20):
        seen["ua"] = req.get_header("User-agent")
        return FakeResp()

    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    ai._call_messages([{"role": "user", "content": "hi"}])

    assert seen["ua"] == ai._USER_AGENT
