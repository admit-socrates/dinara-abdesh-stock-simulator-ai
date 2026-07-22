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

**How the explanations are generated.** They are produced by a deterministic rule engine
(`build_ai_explanation`, `app.py:286`), not by a language model. It buckets the day's percentage change
into up, down, or flat, looks up the ticker's sector, and fills a matching template. There is no model
API involved and no external AI dependency. Calling it an explanation engine is accurate; calling it a
language model would not be.

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
- The explanation engine is rule-based, so it describes patterns rather than analysing a company.
- Nothing here is financial advice, and no security is recommended.
