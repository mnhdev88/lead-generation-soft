"""Logins, sessions, lockouts, admin-only user management and request-forgery protection."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, config
from app.models import User, UserSession, utcnow

from .conftest import TEST_PASSWORD, add_user

NEW_PASSWORD = "another long passphrase"


def login(client, email="admin@example.com", password=TEST_PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


# ----- every data endpoint needs a login ---------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/jobs"),
        ("get", "/api/companies"),
        ("get", "/api/companies/export.csv"),
        ("get", "/api/people"),
        ("get", "/api/people/export.xlsx"),
        ("get", "/api/lists"),
        ("get", "/api/usage"),
        ("get", "/api/sources"),
        ("get", "/api/export/sheets/status"),
        ("post", "/api/emails/verify"),
        ("post", "/api/jobs"),
        ("get", "/api/auth/me"),
        ("get", "/api/users"),
    ],
)
def test_data_needs_login(anon_client, method, path):
    r = getattr(anon_client, method)(path, **({"json": {}} if method == "post" else {}))
    assert r.status_code == 401


def test_health_is_public(anon_client):
    assert anon_client.get("/api/health").json() == {"status": "ok"}


def test_every_api_route_is_protected(anon_client):
    """Catches a new router that forgot the login check."""
    from app.main import app

    public = {"/api/health", "/api/auth/login", "/api/auth/logout"}
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path in public:
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            url = path.replace("{", "").replace("}", "")  # /api/lists/{list_id} -> /api/lists/list_id
            r = anon_client.request(method, url.replace("list_id", "1").replace("user_id", "1")
                                    .replace("company_id", "1").replace("job_id", "1").replace("person_id", "1")
                                    .replace("contact_id", "1"))
            assert r.status_code in (401, 422), f"{method} {path} answered {r.status_code} without a login"


# ----- logging in and out ----------------------------------------------------------


def test_login_sets_httponly_cookie_and_me_works(db, anon_client):
    add_user(db)
    r = login(anon_client, email="  Admin@Example.com ")  # case and spaces don't matter
    assert r.status_code == 200
    assert r.json()["email"] == "admin@example.com"
    cookie = r.headers["set-cookie"]
    assert auth.COOKIE_NAME in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert "password_hash" not in r.text
    assert anon_client.get("/api/auth/me").json()["role"] == "admin"
    assert anon_client.get("/api/jobs").status_code == 200


def test_session_token_is_stored_hashed(db, client):
    token = client.cookies.get(auth.COOKIE_NAME)
    stored = db.query(UserSession).one()
    assert token and stored.token_hash != token and len(stored.token_hash) == 64


def test_wrong_password_and_unknown_email_look_the_same(db, anon_client):
    add_user(db)
    a = login(anon_client, password="wrong password!!")
    b = login(anon_client, email="nobody@example.com")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json() == {"detail": auth.WRONG_LOGIN}


def test_inactive_user_cannot_log_in(db, anon_client):
    add_user(db, is_active=False)
    assert login(anon_client).status_code == 401


def test_logout_ends_the_session(db, client):
    assert client.post("/api/auth/logout").status_code == 204
    assert db.query(UserSession).count() == 0
    assert client.get("/api/jobs").status_code == 401


def test_stolen_cookie_is_useless_after_logout(db, client):
    token = client.cookies.get(auth.COOKIE_NAME)
    client.post("/api/auth/logout")
    client.cookies.set(auth.COOKIE_NAME, token)
    assert client.get("/api/jobs").status_code == 401


def test_made_up_cookie_is_refused(anon_client):
    anon_client.cookies.set(auth.COOKIE_NAME, "made-up-token")
    assert anon_client.get("/api/jobs").status_code == 401


def test_session_expires_when_idle_or_too_old(db, client):
    row = db.query(UserSession).one()
    row.last_seen_at = utcnow() - timedelta(hours=config.SESSION_IDLE_HOURS + 1)
    db.commit()
    assert client.get("/api/jobs").status_code == 401

    row.last_seen_at = utcnow()
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert client.get("/api/jobs").status_code == 401


# ----- brute-force protection --------------------------------------------------------


def test_account_locks_after_too_many_wrong_passwords(db, anon_client):
    add_user(db)
    for _ in range(config.LOGIN_MAX_FAILURES):
        assert login(anon_client, password="wrong password!!").status_code == 401
    # Locked: even the right password is refused for now.
    r = login(anon_client)
    assert r.status_code == 429
    user = db.query(User).one()
    db.refresh(user)
    assert user.locked_until is not None

    # After the lock runs out, the right password works again.
    user.locked_until = utcnow() - timedelta(seconds=1)
    db.commit()
    auth.ip_limiter.reset()
    assert login(anon_client).status_code == 200


def test_one_address_trying_many_accounts_is_slowed_down(anon_client):
    for i in range(config.LOGIN_IP_MAX_FAILURES):
        login(anon_client, email=f"guess{i}@example.com")
    assert login(anon_client, email="another@example.com").status_code == 429


# ----- request-forgery (CSRF) protection ------------------------------------------------


def test_changes_without_dashboard_header_are_blocked(db, client):
    from app.main import app

    with TestClient(app, cookies=dict(client.cookies)) as plain:  # logged in, but no header
        r = plain.post("/api/lists", json={"name": "Hot leads"})
    assert r.status_code == 403
    assert client.post("/api/lists", json={"name": "Hot leads"}).status_code == 201


def test_changes_from_another_website_are_blocked(client):
    r = client.post("/api/lists", json={"name": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/lists", json={"name": "x"}, headers={"Origin": config.FRONTEND_ORIGINS[0]})
    assert r.status_code == 201


def test_security_headers(client):
    r = client.get("/api/jobs")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


# ----- changing your password -------------------------------------------------------------


def test_change_password_logs_out_other_browsers(db, client):
    from app.main import app

    with TestClient(app, headers={"X-Requested-With": "leadgen"}) as other:
        assert login(other).status_code == 200
        r = client.post("/api/auth/password", json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})
        assert r.status_code == 204
        assert client.get("/api/jobs").status_code == 200  # this browser stays logged in
        assert other.get("/api/jobs").status_code == 401  # the other one is logged out
        assert login(other).status_code == 401
        assert login(other, password=NEW_PASSWORD).status_code == 200


@pytest.mark.parametrize(
    "current,new,message",
    [
        ("wrong current pw", NEW_PASSWORD, "current password is wrong"),
        (TEST_PASSWORD, "short", "at least"),
        (TEST_PASSWORD, "aaaaaaaaaaaaaaaa", "too simple"),
        (TEST_PASSWORD, "my admin password", "email name"),
        (TEST_PASSWORD, TEST_PASSWORD, "must be different"),
    ],
)
def test_change_password_rules(client, current, new, message):
    r = client.post("/api/auth/password", json={"current_password": current, "new_password": new})
    assert r.status_code == 400
    assert message in r.json()["detail"]


# ----- user management (admins only) -----------------------------------------------------


def test_admin_adds_user(client):
    r = client.post("/api/users", json={"email": "Sara@Example.com", "name": "Sara", "password": NEW_PASSWORD})
    assert r.status_code == 201
    assert r.json()["email"] == "sara@example.com" and r.json()["role"] == "member"
    assert client.post("/api/users", json={"email": "sara@example.com", "name": "S", "password": NEW_PASSWORD}).status_code == 409
    assert {u["email"] for u in client.get("/api/users").json()} == {"admin@example.com", "sara@example.com"}


def test_members_cannot_manage_users(db, anon_client):
    add_user(db, email="member@example.com", role="member")
    assert login(anon_client, email="member@example.com").status_code == 200
    assert anon_client.get("/api/jobs").status_code == 200  # can use the app
    assert anon_client.get("/api/users").status_code == 403
    r = anon_client.post("/api/users", json={"email": "x@example.com", "name": "X", "password": NEW_PASSWORD})
    assert r.status_code == 403


def test_deactivating_or_resetting_a_user_logs_them_out(db, client):
    from app.main import app

    member = add_user(db, email="member@example.com", role="member")
    with TestClient(app, headers={"X-Requested-With": "leadgen"}) as other:
        assert login(other, email="member@example.com").status_code == 200
        assert client.patch(f"/api/users/{member.id}", json={"password": NEW_PASSWORD}).status_code == 200
        assert other.get("/api/jobs").status_code == 401
        assert login(other, email="member@example.com", password=NEW_PASSWORD).status_code == 200

        assert client.patch(f"/api/users/{member.id}", json={"is_active": False}).status_code == 200
        assert other.get("/api/jobs").status_code == 401
        assert login(other, email="member@example.com", password=NEW_PASSWORD).status_code == 401


def test_admin_cannot_lock_themselves_out(db, client):
    me = client.get("/api/auth/me").json()
    assert client.patch(f"/api/users/{me['id']}", json={"is_active": False}).status_code == 400
    assert client.patch(f"/api/users/{me['id']}", json={"role": "member"}).status_code == 400
    assert client.patch(f"/api/users/{me['id']}", json={"name": "Boss"}).json()["name"] == "Boss"
