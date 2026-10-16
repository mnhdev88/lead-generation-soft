"""Shared test setup.

Before any app module is imported we point the app at a throw-away SQLite file, so tests
never touch backend/data/leads.db, and switch off the background worker thread so it
can't pick up the jobs that tests create (tests call the worker functions directly).
"""
import os
import shutil
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix="leadgen-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR}/test.db"
os.environ["LEADGEN_DISABLE_WORKER"] = "1"

import pytest  # noqa: E402


@pytest.fixture
def db():
    """A fresh, empty database for one test. Yields a session for setting up data."""
    from app.db import Base, SessionLocal, engine, init_db

    Base.metadata.drop_all(engine)
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


TEST_PASSWORD = "correct horse battery"


def add_user(db, email="admin@example.com", role="admin", password=TEST_PASSWORD, **kw):
    from app.auth import hash_password
    from app.models import User

    user = User(email=email, name=email.split("@")[0], role=role, password_hash=hash_password(password), **kw)
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def anon_client(db):
    """FastAPI test client (no background worker) on a fresh database, not logged in.
    Sends the header the dashboard sends, so only the login is missing."""
    from fastapi.testclient import TestClient

    from app.auth import ip_limiter
    from app.main import app

    ip_limiter.reset()
    with TestClient(app, headers={"X-Requested-With": "leadgen"}) as test_client:
        yield test_client


@pytest.fixture
def client(db, anon_client):
    """Like anon_client, but logged in as an admin."""
    add_user(db)
    r = anon_client.post("/api/auth/login", json={"email": "admin@example.com", "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
    return anon_client


def pytest_sessionfinish(session, exitstatus):
    try:
        from app.db import engine

        engine.dispose()  # release the SQLite file so Windows lets us delete it
    except Exception:
        pass
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
