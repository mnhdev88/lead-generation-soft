"""Logins: passwords, sessions and the "who is calling?" check used by every API endpoint.

- Passwords are stored as argon2id hashes (slow on purpose, so stolen hashes are hard to crack).
- Logging in creates a session: a random token in an HttpOnly cookie (JavaScript can't read
  it, so a malicious script can't steal it). The database keeps only the token's SHA-256.
- Sessions end after SESSION_IDLE_HOURS unused, or SESSION_DAYS at the latest, at logout,
  when the password changes, or when an admin deactivates the user.
- Too many wrong passwords lock the account for a while (and slow down one attacker trying
  many accounts from the same address).
"""
import hashlib
import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import config
from .db import get_session
from .models import User, UserSession, utcnow

log = logging.getLogger("auth")

# The "__Host-" prefix makes browsers refuse the cookie unless it is Secure, for this exact
# host and path "/", so a sibling subdomain can't overwrite it. It needs https, so it is only
# used when COOKIE_SECURE=1.
COOKIE_NAME = "__Host-leadgen_session" if config.COOKIE_SECURE else "leadgen_session"
ROLES = ("admin", "member")

_hasher = PasswordHasher()
# Checked when the email is unknown, so a wrong email takes as long as a wrong password
# (otherwise the response time would reveal which emails have an account).
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))
# Write "last seen" at most this often, not on every request.
_TOUCH_EVERY = timedelta(minutes=5)


def _aware(d: datetime) -> datetime:
    # SQLite forgets the timezone; all our times are UTC.
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


# ----- passwords ------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False


def password_problem(password: str, email: str = "") -> str | None:
    """Why this password isn't allowed, or None when it is fine."""
    if len(password) < config.PASSWORD_MIN_LENGTH:
        return f"The password must be at least {config.PASSWORD_MIN_LENGTH} characters long."
    if len(password) > 128:
        return "The password can be at most 128 characters long."
    if len(set(password)) < 5:
        return "The password is too simple. Use a mix of different characters."
    local = normalize_email(email).split("@")[0]
    if len(local) >= 4 and local in password.lower():
        return "The password must not contain your email name."
    return None


# ----- wrong-password limits per address (kept in memory) --------------------------


class _IpLimiter:
    """Remembers recent failed logins per address. Restarting the backend forgets them,
    which is fine: the per-account lock (stored in the database) still applies."""

    def __init__(self):
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, ip: str, now: float) -> deque[float]:
        window = config.LOGIN_LOCK_MINUTES * 60
        fails = self._fails[ip]
        while fails and now - fails[0] > window:
            fails.popleft()
        return fails

    def blocked(self, ip: str) -> bool:
        with self._lock:
            return len(self._recent(ip, time.monotonic())) >= config.LOGIN_IP_MAX_FAILURES

    def fail(self, ip: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._recent(ip, now).append(now)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()


ip_limiter = _IpLimiter()


# ----- logging in and out ----------------------------------------------------------


class LoginError(Exception):
    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


WRONG_LOGIN = "Wrong email or password."
TOO_MANY = f"Too many failed attempts. Try again in {config.LOGIN_LOCK_MINUTES} minutes."


def authenticate(session: Session, email: str, password: str, ip: str) -> User:
    """The user for this email + password, or LoginError. Counts failures and locks."""
    if ip_limiter.blocked(ip):
        raise LoginError(TOO_MANY, 429)
    email = normalize_email(email)
    user = session.scalar(select(User).where(User.email == email))
    now = utcnow()

    if user and user.locked_until and _aware(user.locked_until) > now:
        verify_password(None, password)  # same timing as a normal attempt
        ip_limiter.fail(ip)
        log.warning("Login refused (account locked): %s from %s", email, ip)
        raise LoginError(TOO_MANY, 429)

    ok = verify_password(user.password_hash if user else None, password)
    if not ok or not user or not user.is_active:
        ip_limiter.fail(ip)
        if user and not ok:
            user.failed_logins += 1
            if user.failed_logins >= config.LOGIN_MAX_FAILURES:
                user.locked_until = now + timedelta(minutes=config.LOGIN_LOCK_MINUTES)
                user.failed_logins = 0
                log.warning("Account locked after too many wrong passwords: %s", email)
            session.commit()
        log.warning("Login failed: %s from %s", email, ip)
        raise LoginError(WRONG_LOGIN)

    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    session.commit()
    log.info("Login: %s from %s", email, ip)
    return user


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(session: Session, user: User, request: Request, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session.add(
        UserSession(
            token_hash=_token_hash(token),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=config.SESSION_DAYS),
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:300],
        )
    )
    # Housekeeping: forget this user's expired sessions.
    session.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.expires_at < now))
    session.commit()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=config.SESSION_DAYS * 86400,
        path="/",
        secure=config.COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )


def end_session(session: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        session.execute(delete(UserSession).where(UserSession.token_hash == _token_hash(token)))
        session.commit()
    response.delete_cookie(COOKIE_NAME, path="/", secure=config.COOKIE_SECURE, httponly=True, samesite="lax")


def end_all_sessions(session: Session, user_id: int, keep_token: str | None = None) -> None:
    """Log a user out everywhere (optionally except the browser making this request)."""
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if keep_token:
        stmt = stmt.where(UserSession.token_hash != _token_hash(keep_token))
    session.execute(stmt)


def client_ip(request: Request) -> str:
    # Behind nginx, uvicorn's --proxy-headers puts the visitor's real address here.
    return request.client.host if request.client else "unknown"


# ----- FastAPI dependencies --------------------------------------------------------

NOT_LOGGED_IN = HTTPException(status_code=401, detail="Please log in.")


def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    """The logged-in user, or 401. Every data endpoint depends on this."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise NOT_LOGGED_IN
    row = session.scalar(select(UserSession).where(UserSession.token_hash == _token_hash(token)))
    now = utcnow()
    if (
        row is None
        or _aware(row.expires_at) <= now
        or now - _aware(row.last_seen_at) > timedelta(hours=config.SESSION_IDLE_HOURS)
    ):
        raise NOT_LOGGED_IN
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise NOT_LOGGED_IN
    if now - _aware(row.last_seen_at) > _TOUCH_EVERY:
        row.last_seen_at = now
        session.commit()
    request.state.user = user
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can do this.")
    return user
