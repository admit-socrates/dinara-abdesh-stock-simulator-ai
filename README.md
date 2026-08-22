<!-- ADMIT case: cases/dinara-abdesh -->
# StockSim AI

A stock-market learning simulator for students. Trade 50 real tickers with $10,000 of virtual money,
then read an explanation of *why* the market moved and what question to ask about it.

Built by Dinara Abdesh. Live at https://stock-simulator-ai.vercel.app

## What makes it different

Most trading simulators teach you the mechanics of buying and selling. This one adds an **explanation
layer**: on every stock, in the dashboard, the market list, the portfolio, and a dedicated coach page,
it produces a headline, a plausible reason for the day's move, the matching risk signal, and a takeaway
question for the student.

The idea is that a beginner who buys a falling stock should be asked "is this a real business problem
or short-term market fear?" rather than just watching a red number.

**How the explanations are generated.** Two paths, and the app tells you which one is running.

1. **Language model (current default in production).** When a provider key is set, `ai.py` calls a real
   LLM over plain REST from the standard library: Groq first (`GROQ_API_KEY`), Google Gemini as the
   fallback (`GEMINI_API_KEY`). The model is given only the student's real portfolio numbers and cached
   prices, and the system prompt forbids inventing news, earnings, or price targets. It also powers the
   chat on the AI Coach page. Per-user daily cap: `AI_DAILY_LIMIT`, default 40.
2. **Deterministic rule engine (fallback).** With no key set, or if the model call fails or times out,
   the app falls back to `build_ai_explanation` in `app.py`: bucket the day's percentage change into up,
   down, or flat, look up the ticker's sector, fill a matching template. No network call is made.

`GET /healthz` reports which is active (`ai_coach`, `ai_provider`), and the AI Coach page carries a
badge saying "AI live" or "rule-based". Describe the project by whichever one is actually running.

## Pages

`/login` · `/dashboard` · `/market` · `/portfolio` · `/leaderboard` · `/ai-coach` · `/admin`

## Stack

Python 3.11, Flask, Flask-Login, SQLite locally and Supabase Postgres in production, yfinance for
quotes, Chart.js for charts. Server-rendered Jinja templates, one stylesheet, no build step.

## Running it locally

```bash
pip install -r requirements.txt
python app.py
```

It defaults to a local SQLite file (`stock_simulator.db`). Nothing else is needed to try it.

## Running it in production (Vercel)

Set these as environment variables in the Vercel project, never in the repo:

```
DATABASE_URL=<Supabase TRANSACTION POOLER connection string>
SECRET_KEY=<a long random secret>
ADMIN_PASSWORDS=<comma-separated admin passwords>
```

### Use the pooler URL, not the direct one

Supabase gives you two connection strings. Use the **Transaction Pooler** one.

- Pooler (correct): `postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:6543/postgres`
- Direct (breaks): `postgresql://postgres:<pw>@db.<ref>.supabase.co:5432/postgres`

Vercel's serverless functions cannot reach Supabase's direct IPv6 endpoint. The app knows this and
deliberately refuses the direct form (`database.py:43-46`), blanking it rather than hanging.

**What happens if you get this wrong:** `IS_POSTGRES` becomes false, `SESSION_DEMO_MODE` turns on
(`app.py:39`), and the app silently degrades to a demo where any username and any 4-character password
starts a throwaway funded session. Nothing is saved, and the leaderboard is always empty. It looks like
a broken login. It is actually a missing database.

**How to check which mode you're in:** register a user, log out, then log in again with the *wrong*
password. If it lets you in, you are in demo mode. If it rejects you, the database is connected.

## Data and privacy

No real money, no real brokerage, no payment details. Prices come from a public quotes API and refresh
once a day via a scheduled job (`/api/cron/refresh-prices`). Accounts store a username and a password
hash (pbkdf2:sha256) and nothing else.

## Known limitations

- Prices refresh daily, not live, so intraday moves are not reflected.
- Without a provider key the explanation engine falls back to rules, so it describes patterns
  rather than analysing a company. Check `/healthz` to see which mode is live.
- The model only sees the portfolio numbers and cached prices passed to it. It has no news feed,
  so it reasons in general terms rather than citing events.
- Nothing here is financial advice, and no security is recommended.
