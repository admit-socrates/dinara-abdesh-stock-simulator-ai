"""
Test setup for StockSim AI.

We force a local, network-free environment:
  * no VERCEL / no DATABASE_URL  -> SQLite backend, real auth (not demo mode)
  * no ANTHROPIC_API_KEY         -> AI Coach defaults to the rule-based fallback
  * prices are seeded by hand    -> yfinance is never called
"""

import os

# Must be set BEFORE importing the app modules.
for _k in ("VERCEL", "DATABASE_URL", "SUPABASE_DB_URL",
           "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(_k, None)

import pytest  # noqa: E402

import database  # noqa: E402
import app as app_module  # noqa: E402

# Deterministic prices so buy/sell math is easy to assert.
SEED_PRICES = [
    ("AAPL", "Apple Inc.", 100.0, 98.0, 2.04),
    ("MSFT", "Microsoft Corp.", 200.0, 205.0, -2.44),
    ("TSLA", "Tesla Inc.", 50.0, 50.0, 0.0),
    ("NVDA", "NVIDIA Corp.", 400.0, 380.0, 5.26),
]


def _seed_prices():
    conn = database.get_connection()
    try:
        for ticker, name, price, prev, chg in SEED_PRICES:
            conn.execute(
                "INSERT INTO stock_prices (ticker, name, price, prev_close, change_pct) "
                "VALUES (?, ?, ?, ?, ?)",
                (ticker, name, price, prev, chg),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Isolated DB file per test.
    monkeypatch.setattr(database, "SQLITE_FILE", str(tmp_path / "test.db"))
    app_module.app.config.update(TESTING=True)

    database.init_db(app_module.app)
    _seed_prices()

    # Skip the before_request bootstrap (which would call yfinance).
    monkeypatch.setattr(app_module, "_runtime_ready", True)

    with app_module.app.test_client() as c:
        yield c


def register(client, username="student1", password="secret1"):
    return client.post(
        "/login",
        data={"action": "register", "username": username, "password": password},
        follow_redirects=True,
    )


def login(client, username="student1", password="secret1"):
    return client.post(
        "/login",
        data={"action": "login", "username": username, "password": password},
        follow_redirects=True,
    )
