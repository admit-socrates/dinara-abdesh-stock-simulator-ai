"""
Real AI layer for StockSim AI — backed by Google Gemini (free tier).

Two jobs:
  1. explain_stock(...)  — a short, student-level explanation of why a stock
     may be moving, grounded ONLY in the real price data we pass in.
  2. coach_answer(...)   — answer a student's free-text question, grounded in
     their real portfolio (cash, holdings, P&L) and live prices, with short
     conversation memory for follow-ups.

Why Gemini: it has a genuinely free API tier with no credit card required,
which fits a student pilot. The call is a plain REST request over the Python
standard library, so there is no extra dependency to install.

Safety rules that keep this cheap and honest:
  * Graceful fallback. If GEMINI_API_KEY is missing or any call fails/times
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

# Models tried in order; each has its OWN free-tier quota bucket, so if one is
# rate-limited (429) or unavailable (404) we fall through to the next. Override
# the first choice with the AI_MODEL env var.
DEFAULT_MODELS = ["gemini-2.0-flash-lite", "gemini-2.0-flash"]
_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"


def model_candidates():
    env = os.environ.get("AI_MODEL", "").strip()
    if env:
        return [env] + [m for m in DEFAULT_MODELS if m != env]
    return DEFAULT_MODELS

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


def _api_key():
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")).strip()


def ai_available():
    """True if we have a Gemini key configured."""
    return bool(_api_key())


def model_name():
    return model_candidates()[0]


def _call_messages(messages, max_tokens=320):
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
    for model in model_candidates():
        url = f"{_API_ROOT}/{model}:generateContent?key={_api_key()}"
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
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
            if e.code in (400, 404, 429, 503):  # quota / unavailable → try next model
                continue
            raise last_err
        except Exception as e:
            logger.error("Gemini call failed on %s: %r", model, e)
            attempts.append(f"{model}=ERR")
            last_err = e
            continue
    raise last_err or RuntimeError("All Gemini models failed")


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
