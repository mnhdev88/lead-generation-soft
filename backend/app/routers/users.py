"""Admins manage who can log in. Users are deactivated, never deleted, so the history of
who did what stays readable later."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth
from ..db import get_session
from ..models import User, utcnow
from ..schemas import UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[UserOut])
def list_users(session: Session = Depends(get_session), _: User = Depends(auth.require_admin)):
    return session.scalars(select(User).order_by(User.name)).all()


@router.post("", response_model=UserOut, status_code=201)
def create_user(body: UserCreate, session: Session = Depends(get_session), _: User = Depends(auth.require_admin)):
    if session.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(status_code=409, detail="A user with this email already exists.")
    problem = auth.password_problem(body.password, body.email)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    user = User(email=body.email, name=body.name.strip(), role=body.role,
                password_hash=auth.hash_password(body.password))
    session.add(user)
    session.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UserUpdate,
    session: Session = Depends(get_session),
    admin: User = Depends(auth.require_admin),
):
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Stops an admin from locking everyone out by accident.
    if user.id == admin.id and (body.is_active is False or body.role == "member"):
        raise HTTPException(status_code=400, detail="You can't deactivate yourself or remove your own admin role.")

    if body.name is not None:
        user.name = body.name.strip()
    if body.role is not None:
        user.role = body.role
    if body.password is not None:
        problem = auth.password_problem(body.password, user.email)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        user.password_hash = auth.hash_password(body.password)
        user.password_changed_at = utcnow()
        user.failed_logins = 0
        user.locked_until = None
    if body.is_active is not None:
        user.is_active = body.is_active
    # A new password or deactivation logs the user out everywhere.
    if body.password is not None or body.is_active is False:
        auth.end_all_sessions(session, user.id)
    session.commit()
    return user
