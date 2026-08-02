"""Authentication: registration, login, and the security-critical rejections."""

from conftest import register, login


def test_register_creates_funded_account(client):
    resp = register(client)
    assert resp.status_code == 200
    # Dashboard shows the starting cash.
    cash = client.get("/api/cash").get_json()["cash"]
    assert cash == 10_000.0


def test_wrong_password_is_rejected(client):
    register(client, "alice", "rightpass")
    client.get("/logout")
    resp = login(client, "alice", "wrongpass")
    assert b"Invalid username or password" in resp.data
    # Still logged out: hitting a protected API redirects to login.
    r = client.get("/api/cash", follow_redirects=False)
    assert r.status_code in (302, 401)


def test_nonexistent_user_is_rejected(client):
    resp = login(client, "ghost", "whatever")
    assert b"Invalid username or password" in resp.data


def test_duplicate_username_blocked(client):
    register(client, "bob", "pass1234")
    client.get("/logout")
    resp = register(client, "bob", "otherpass")
    assert b"already taken" in resp.data


def test_short_credentials_rejected(client):
    resp = client.post(
        "/login",
        data={"action": "register", "username": "ab", "password": "x"},
        follow_redirects=True,
    )
    assert b"at least 3 characters" in resp.data
