"""Create a login (or reset its password) from the command line.

    python -m app.create_user               asks for everything, makes an admin
    python -m app.create_user --member      makes a normal user

The password is typed hidden and never shown. Use this for the first admin; after that,
admins can add users on the Users page of the dashboard.
"""
import getpass
import sys

from sqlalchemy import select

from . import auth
from .db import SessionLocal, init_db
from .models import User, utcnow


def main() -> int:
    role = "member" if "--member" in sys.argv else "admin"
    init_db()
    email = auth.normalize_email(input("Email: "))
    if "@" not in email:
        print("That doesn't look like an email address.")
        return 1
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user:
            print(f"{email} already exists. Typing a new password will reset it.")
            name = user.name
        else:
            name = input("Name: ").strip() or email.split("@")[0]
        while True:
            password = getpass.getpass(f"Password (at least {auth.config.PASSWORD_MIN_LENGTH} characters, hidden): ")
            problem = auth.password_problem(password, email)
            if problem:
                print(problem)
                continue
            if getpass.getpass("Same password again: ") != password:
                print("The two passwords are different. Try again.")
                continue
            break
        if user:
            user.password_hash = auth.hash_password(password)
            user.password_changed_at = utcnow()
            user.failed_logins = 0
            user.locked_until = None
            user.is_active = True
            auth.end_all_sessions(session, user.id)
        else:
            user = User(email=email, name=name, role=role, password_hash=auth.hash_password(password))
            session.add(user)
        session.commit()
        print(f"Done. {email} ({user.role}) can now log in.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
