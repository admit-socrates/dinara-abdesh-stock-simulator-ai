"""
Real AI layer for StockSim AI — backed by a free LLM (Groq preferred, Google
Gemini as fallback).

Two jobs:
  1. explain_stock(...)  — a short, student-level explanation of why a stock
     may be moving, grounded ONLY in the real price data we pass in.
  2. coach_answer(...)   — answer a student's free-text question, grounded in
     their real portfolio (cash, holdings, P&L) and live prices, with short
     conversation memory for follow-ups.

Why these two: both have a genuinely free API tier with no credit card
required, which fits a student pilot. Groq is preferred because its free limits
are far more generous. Each call is a plain REST request over the Python standard
library, so there is no extra dependency to install.

Safety rules that keep this cheap and honest:
  * Graceful fallback. If no provider key is set, or any call fails/times
    out, we fall back to the deterministic explanation in
    app.build_ai_explanation. The app never breaks because the AI is down.
  * No invented facts. The system prompt forbids inventing specific news,
    numbers, or price targets — the model may only reason about the data given.
  * Not financial advice. Everything is framed as educational, on virtual money.
"""

import json
import logging
import os
import urllib.request
import urllib.error

logger = logging.getLogger("stocksim.ai")

# Two free backends are supported. Whichever key is set wins; Groq is tried
# first because its free tier is far more generous than Gemini's (which 429s
# after only a handful of calls). Neither needs a credit card. Within a
# provider we try models in order — each has its OWN quota bucket — so a 429/404
# on the first falls through to the next. Override the first model with AI_MODEL.
# Groq retires models on a schedule and returns 404 model_not_found for a dead id.
# These are the documented replacements for the llama-3.x pair that was decommissioned
# (llama-3.3-70b-versatile -> openai/gpt-oss-120b, llama-3.1-8b-instant -> openai/gpt-oss-20b,
# per https://console.groq.com/docs/deprecations, read 2026-08-22). They are only the
# preference order: if they ever die too, _groq_candidates() asks the API what exists now.
GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
GEMINI_MODELS = ["gemini-2.0-flash-lite", "gemini-2.0-flash"]
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
_GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

# Groq sits behind Cloudflare, which rejects the Python standard library's default
# User-Agent ("Python-urllib/3.x") with HTTP 403 "error code: 1010" before the request
# ever reaches the API. Any explicit User-Agent passes. Verified 2026-08-22: the same
# request with the default UA returns 403/1010 and with this one returns a normal API
# response. Sent on every provider call so a future edge rule cannot bite us again.
_USER_AGENT = "StockSimAI/1.0 (+https://stock-simulator-ai.vercel.app)"


def _groq_key():
    return os.environ.get("GROQ_API_KEY", "").strip()


def _gemini_key():
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")).strip()


def provider_name():
    """Which backend is active: 'groq', 'gemini', or None if no key is set."""
    if _groq_key():
        return "groq"
    if _gemini_key():
        return "gemini"
    return None


def _env_first(defaults):
    """Put the AI_MODEL override (if any) at the front of a provider's list."""
    env = os.environ.get("AI_MODEL", "").strip()
    if env:
        return [env] + [m for m in defaults if m != env]
    return list(defaults)


def _models_for(provider):
    return _env_first(GROQ_MODELS if provider == "groq" else GEMINI_MODELS)


# Cached per process. None = not asked yet, [] = asked and it failed.
_groq_live_cache = None
# Model families Groq serves that cannot answer a chat message.
_NOT_CHAT = ("whisper", "tts", "guard", "embed", "vision-preview")


def _groq_live_models():
    """Ask Groq which models the key can actually use, right now.

    Hardcoded model ids rot: Groq decommissions them and every call then returns
    404 model_not_found, which the coach surfaces as "busy" (this happened
    2026-08-22 with the llama-3.x pair). Asking the API removes the whole class
    of failure. Cached for the life of the process; never fatal.
    """
    global _groq_live_cache
    if _groq_live_cache is not None:
        return _groq_live_cache
    _groq_live_cache = []
    try:
        req = urllib.request.Request(
            _GROQ_MODELS_URL,
            headers={"Authorization": f"Bearer {_groq_key()}", "User-Agent": _USER_AGENT},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ids = [m.get("id", "") for m in body.get("data", []) if m.get("id")]
        _groq_live_cache = [m for m in ids if not any(bad in m.lower() for bad in _NOT_CHAT)]
        # Logged so the model list can be read from the deploy logs without
        # anyone handling the API key.
        logger.info("Groq live chat models: %s", ", ".join(_groq_live_cache) or "(none)")
    except Exception as e:
        logger.error("Could not list Groq models: %r", e)
    return _groq_live_cache


def _groq_candidates():
    """Preferred models first, then anything else the key can actually reach."""
    preferred = _env_first(GROQ_MODELS)
    live = _groq_live_models()
    if not live:
        return preferred
    ordered = [m for m in preferred if m in live]
    ordered += [m for m in live if m not in ordered]
    return ordered or preferred

_SYSTEM_PROMPT = (
    "You are the AI Coach inside StockSim AI, a stock-market SIMULATOR used by "
    "high-school students in Kazakhstan to learn investing with virtual money. "
    "Your job is to teach reasoning, not to give financial advice.\n\n"
    "Hard rules:\n"
    "- This is a simulation with fake money. Never tell the student what they "
    "'should' buy or sell as real advice. Frame everything as learning.\n"
    "- Only use the numbers and facts given to you in the message. NEVER invent "
    "specific news headlines, earnings figures, analyst targets, or prices. If "
    "you don't have a fact, reason about it in general terms instead.\n"
    "- Be short and clear. A 15-year-old should understand every sentence. "
    "Avoid jargon; when you must use a term (like 'volatility'), explain it in "
    "a few words.\n"
    "- Encourage the student to think: connect the price move to sectors, risk, "
    "and what question they should ask next.\n"
    "- Keep answers to about 90 words unless the student asks for more."
)


def ai_available():
    """True if any AI provider key is configured."""
    return provider_name() is not None


def model_name():
    """First model of the active provider, or None if no provider is configured."""
    provider = provider_name()
    return _models_for(provider)[0] if provider else None


def _call_messages(messages, max_tokens=320):
    """Dispatch a multi-turn call to the active provider. Returns text or raises."""
    provider = provider_name()
    if provider == "groq":
        return _call_groq(messages, max_tokens)
    if provider == "gemini":
        return _call_gemini(messages, max_tokens)
    raise RuntimeError("No AI provider configured")


def _call_gemini(messages, max_tokens=320):
    """
    Multi-turn call to Gemini. `messages` is a list of {role, content} where
    role is 'user' or 'assistant'. Returns the text, or raises on failure.
    """
    contents = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})

    payload = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.4},
    }
    data = json.dumps(payload).encode("utf-8")

    last_err = None
    attempts = []
    for model in _models_for("gemini"):
        url = f"{_GEMINI_ROOT}/{model}:generateContent?key={_gemini_key()}"
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            candidates = body.get("candidates") or []
            parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
            text = "".join(p.get("text", "") for p in parts).strip()
            if not text:
                raise ValueError("Empty response")
            return text
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            logger.error("Gemini %s on %s: %s", e.code, model, detail)
            attempts.append(f"{model}={e.code}")
            last_err = RuntimeError(f"attempts[{', '.join(attempts)}] last={detail}")
            if e.code in (400, 403, 404, 429, 503):  # blocked / quota / unavailable → try next model
                continue
            raise last_err
        except Exception as e:
            logger.error("Gemini call failed on %s: %r", model, e)
            attempts.append(f"{model}=ERR")
            last_err = e
            continue
    raise last_err or RuntimeError("All Gemini models failed")


def _call_groq(messages, max_tokens=320):
    """Multi-turn call to Groq's OpenAI-compatible chat API. Returns text or raises."""
    chat = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for m in messages:
        role = "assistant" if m.get("role") == "assistant" else "user"
        chat.append({"role": role, "content": m.get("content", "")})

    last_err = None
    attempts = []
    for model in _groq_candidates():
        payload = {
            "model": model,
            "messages": chat,
            "max_tokens": max_tokens,
            "temperature": 0.4,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _GROQ_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_groq_key()}",
                "User-Agent": _USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            choices = body.get("choices") or []
            text = (choices[0].get("message", {}).get("content", "") if choices else "").strip()
            if not text:
                raise ValueError("Empty response")
            return text
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            logger.error("Groq %s on %s: %s", e.code, model, detail)
            attempts.append(f"{model}={e.code}")
            last_err = RuntimeError(f"attempts[{', '.join(attempts)}] last={detail}")
            if e.code in (400, 403, 404, 429, 503):  # blocked / quota / unavailable → try next model
                continue
            raise last_err
        except Exception as e:
            logger.error("Groq call failed on %s: %r", model, e)
            attempts.append(f"{model}=ERR")
            last_err = e
            continue
    raise last_err or RuntimeError("All Groq models failed")


def _call(prompt, max_tokens=320):
    """Single-turn convenience wrapper."""
    return _call_messages([{"role": "user", "content": prompt}], max_tokens)


def _sanitize_history(history):
    """Keep only well-formed user/assistant text turns from prior exchanges."""
    clean = []
    for turn in history or []:
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            clean.append({"role": role, "content": content.strip()[:1500]})
    return clean[-8:]


# ---------------------------------------------------------------------------
# 1. Per-stock explanation (used on the AI Coach page for the selected stock)
# ---------------------------------------------------------------------------

def explain_stock(stock, holding=None, fallback=None):
    """
    Return a dict with an LLM-written `why` and `student_takeaway`, merged on
    top of the deterministic `fallback` dict so the template still has every
    field it expects. If the AI is unavailable or errors, return `fallback`
    unchanged (with ai_source='rules').
    """
    if fallback is None:
        fallback = {}
    if not ai_available():
        out = dict(fallback)
        out["ai_source"] = "rules"
        return out

    ticker = stock.get("ticker")
    name = stock.get("name") or ticker
    change = stock.get("change_pct")
    price = stock.get("price")
    sector = fallback.get("sector", "public markets")

    change_txt = "no fresh day-change data" if change is None else f"{change:+.2f}% today"
    price_txt = "unknown" if price is None else f"${price:,.2f}"
    owned_txt = ""
    if holding:
        owned_txt = (
            f"\nThe student OWNS this: {holding.get('shares')} shares, "
            f"currently {holding.get('gain_pct', 0):+.2f}% vs their average cost."
        )

    prompt = (
        f"Explain this stock move to a student.\n"
        f"Stock: {name} ({ticker})\n"
        f"Sector: {sector}\n"
        f"Current price: {price_txt}\n"
        f"Day change: {change_txt}{owned_txt}\n\n"
        f"Write two short parts:\n"
        f"WHY: one or two sentences on what KIND of thing could drive a move "
        f"like this for this sector (no invented specific news).\n"
        f"TAKEAWAY: one sentence telling the student what to think about or "
        f"check next.\n"
        f"Format exactly as:\nWHY: ...\nTAKEAWAY: ..."
    )

    try:
        text = _call(prompt, max_tokens=220)
    except Exception:
        out = dict(fallback)
        out["ai_source"] = "rules"
        return out

    why, takeaway = _split_why_takeaway(text)
    out = dict(fallback)
    if why:
        out["why"] = why
    if takeaway:
        out["student_takeaway"] = takeaway
    out["ai_source"] = "llm"
    return out


def _split_why_takeaway(text):
    why, takeaway = None, None
    for line in text.splitlines():
        s = line.strip()
        upper = s.upper()
        if upper.startswith("WHY:"):
            why = s[4:].strip()
        elif upper.startswith("TAKEAWAY:"):
            takeaway = s[9:].strip()
    if why is None and takeaway is None and text:
        why = text.strip()
    return why, takeaway


# ---------------------------------------------------------------------------
# 2. Free-text coach chat (grounded in the student's real portfolio)
# ---------------------------------------------------------------------------

def coach_answer(question, portfolio, holdings, movers, history=None):
    """
    Answer a student's question, grounded in their real portfolio + live prices.

    portfolio: dict with cash, total, pnl, pnl_pct
    holdings:  list of dicts (ticker, shares, gain_pct, current_price)
    movers:    list of dicts (ticker, change_pct) — a few notable movers today
    history:   optional list of prior {role, content} turns for follow-ups

    Returns (answer_text, error_or_None). On any failure returns
    (None, short_message) so the caller can show a friendly note.
    """
    if not ai_available():
        return None, "The AI coach is not switched on yet."

    question = (question or "").strip()
    if not question:
        return None, "Please type a question first."
    if len(question) > 500:
        question = question[:500]

    if holdings:
        holdings_txt = ", ".join(
            f"{h['ticker']} ({h.get('shares')} sh, {h.get('gain_pct', 0):+.1f}%)"
            for h in holdings[:12]
        )
    else:
        holdings_txt = "none yet (all cash)"

    movers_txt = (
        ", ".join(f"{m['ticker']} {m.get('change_pct', 0):+.1f}%" for m in (movers or [])[:6])
        or "no notable movers cached"
    )

    context = (
        f"Here is the student's real simulator state right now:\n"
        f"- Cash: ${portfolio.get('cash', 0):,.2f}\n"
        f"- Total portfolio value: ${portfolio.get('total', 0):,.2f}\n"
        f"- Profit/loss: ${portfolio.get('pnl', 0):,.2f} "
        f"({portfolio.get('pnl_pct', 0):+.2f}%)\n"
        f"- Holdings: {holdings_txt}\n"
        f"- Some movers today: {movers_txt}\n\n"
        f"Student's question: {question}\n\n"
        f"Answer using ONLY the state above and general investing reasoning. "
        f"Do not invent prices or news."
    )

    messages = _sanitize_history(history) + [{"role": "user", "content": context}]
    try:
        text = _call_messages(messages, max_tokens=380)
    except Exception:
        return None, "The AI coach is busy right now. Please try again in a moment."
    return text, None
