"""
Evidence-capture endpoint (/metrics.json): a dated usage snapshot for the
tech-project Impact track. Admin-gated; DB-agnostic funnel + daily series.
"""

from conftest import register


def test_metrics_requires_admin(client):
    r = client.get("/metrics.json")
    assert r.status_code == 403
    assert "admin" in r.get_json()["error"]


def test_metrics_snapshot_counts_real_usage(client):
    # One registered user who places one trade = registered 1, activated 1.
    register(client)
    client.post("/api/buy", json={"ticker": "AAPL", "shares": 5})

    # Authenticate as admin (default local password set).
    client.post("/admin", data={"password": "admin123"}, follow_redirects=True)

    snap = client.get("/metrics.json").get_json()
    assert snap["funnel"]["registered"] == 1
    assert snap["funnel"]["activated"] == 1
    assert snap["funnel"]["active_last_7d"] == 1
    assert snap["funnel"]["activation_rate"] == 1.0
    assert snap["trades"]["total"] == 1
    assert snap["generated_at"].endswith("Z")
    # Daily series spans the window and is shaped correctly.
    assert len(snap["daily"]) == snap["window_days"] + 1
    assert all({"date", "signups", "trades"} <= set(d) for d in snap["daily"])
    assert sum(d["signups"] for d in snap["daily"]) == 1
    assert sum(d["trades"] for d in snap["daily"]) == 1


def test_metrics_activation_rate_with_inactive_user(client):
    # Two registered, one trades -> activation_rate 0.5.
    register(client, username="alpha", password="pw1aaaa")
    client.post("/api/buy", json={"ticker": "AAPL", "shares": 1})
    client.get("/logout")
    register(client, username="beta", password="pw2bbbb")  # registers, never trades

    client.post("/admin", data={"password": "admin123"}, follow_redirects=True)
    snap = client.get("/metrics.json").get_json()
    assert snap["funnel"]["registered"] == 2
    assert snap["funnel"]["activated"] == 1
    assert snap["funnel"]["activation_rate"] == 0.5
