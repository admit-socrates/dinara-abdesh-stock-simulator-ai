"""
Real AI layer for StockSim AI.

This module turns the "AI Coach" from a fixed template into an actual language
model (Anthropic's Claude). It does two jobs:

  1. explain_stock(...)  — write a short, student-level explanation of why a
     stock may be moving, grounded ONLY in the real price data we pass in.
  2. coach_answer(...)   — answer a student's free-text question, grounded in
     their real portfolio (cash, holdings, P&L) and live prices.

Design rules that keep this safe and cheap:

  * Graceful fallback. If ANTHROPIC_API_KEY is missing, the `anthropic`
    package is not installed, or any API call fails/times out, we fall back to
    the deterministic explanation in app.build_ai_explanation. The app never
    breaks just because the AI is unavailable — it simply degrades to the old
    rule-based coach. This is why the site is safe to deploy before the key is
    set.
  * No invented facts. The system prompt forbids inventing specific news,
    numbers, or price targets. The model may only reason about the data we give
    it. This mirrors the engine's core rule: do not fabricate.
  * Not financial advice. Everything is framed as educational, on virtual money.
  * Cheap by default. Small max_tokens, a low-cost model, and a short timeout.
    Per-user daily quotas live in the app/database layer, not here.
"""

import os

# The model can be overridden with the AI_MODEL env var. Haiku is the default:
# it is the cheapest current Claude model, which matters for a public pilot.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

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
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def ai_available():
    """True only if we have a key AND the anthropic SDK is importable."""
    if not _api_key():
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def model_name():
    return os.environ.get("AI_MODEL", "").strip() or DEFAULT_MODEL


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=_api_key(), timeout=20.0)


def _call_messages(messages, max_tokens=320):
    """Multi-turn call. `messages` is a list of {role, content}. Raises on failure."""
    client = _client()
    msg = client.messages.create(
        model=model_name(),
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        messages=messages,
    )
    parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
    text = "".join(parts).strip()
    if not text:
        raise ValueError("Empty response from model")
    return text


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
        # Model didn't follow the format; use the whole thing as the "why".
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
