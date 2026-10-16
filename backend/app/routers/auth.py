"""Log in, log out, "who am I?" and changing your own password."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from .. import auth
from ..db import get_session
from ..models import User, utcnow
from ..schemas import LoginRequest, PasswordChange, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=UserOut)
def login(body: LoginRequest, request: Request, response: Response, session: Session = Depends(get_session)):
    try:
        user = auth.authenticate(session, body.email, body.password, auth.client_ip(request))
    except auth.LoginError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from None
    auth.start_session(session, user, request, response)
    return user


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, session: Session = Depends(get_session)):
    auth.end_session(session, request, response)
    response.status_code = 204
    return response


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(auth.current_user)):
    return user


@router.post("/password", status_code=204)
def change_password(
    body: PasswordChange,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(auth.current_user),
):
    """Change your own password. Logs you out on every other browser."""
    if not auth.verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=400, detail="Your current password is wrong.")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="The new password must be different.")
    problem = auth.password_problem(body.new_password, user.email)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    user.password_hash = auth.hash_password(body.new_password)
    user.password_changed_at = utcnow()
    auth.end_all_sessions(session, user.id, keep_token=request.cookies.get(auth.COOKIE_NAME))
    session.commit()
    return Response(status_code=204)
