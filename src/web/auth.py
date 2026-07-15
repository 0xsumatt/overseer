"""Authentication — Flask-Login over the users table.

No self-registration: accounts exist only if created via the `flask create-user`
CLI (see web/__init__.py). Role gating is a plain boolean flag on the user
(`can_view_internal`) checked by the `role_required` decorator — deliberately
simple for a handful of internal accounts.

Passwords use werkzeug's scrypt-based generate_password_hash. All queries run
through the app's shared ReadStorage pool (auth reads/writes are tiny and rare;
they don't justify a second pool).
"""

from __future__ import annotations

from functools import wraps

from flask import abort
from flask_login import LoginManager, UserMixin, current_user, login_required
from werkzeug.security import check_password_hash, generate_password_hash

from storage.queries import ReadStorage

login_manager = LoginManager()
login_manager.login_view = "pages.login"


class User(UserMixin):
    def __init__(self, id: int, email: str, can_view_internal: bool) -> None:
        self.id = id
        self.email = email
        self.can_view_internal = can_view_internal


def _row_to_user(row) -> User:
    return User(row["id"], row["email"], row["can_view_internal"])


def init_auth(app, storage: ReadStorage) -> None:
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        rows = storage._fetch(
            "SELECT id, email, can_view_internal FROM users WHERE id = %s",
            (int(user_id),),
        )
        return _row_to_user(rows[0]) if rows else None


def verify_credentials(storage: ReadStorage, email: str, password: str) -> User | None:
    rows = storage._fetch(
        "SELECT id, email, password_hash, can_view_internal FROM users WHERE email = %s",
        (email.strip().lower(),),
    )
    if rows and check_password_hash(rows[0]["password_hash"], password):
        return _row_to_user(rows[0])
    return None


def create_user(storage: ReadStorage, email: str, password: str, internal: bool) -> None:
    """Insert (or update the password/role of) an account. CLI-only entry point."""
    with storage._pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO users (email, password_hash, can_view_internal)
            VALUES (%s, %s, %s)
            ON CONFLICT (email) DO UPDATE
                SET password_hash = EXCLUDED.password_hash,
                    can_view_internal = EXCLUDED.can_view_internal
            """,
            (email.strip().lower(), generate_password_hash(password), internal),
        )


def role_required(flag: str):
    """@role_required("can_view_internal") — 403 unless the flag is set."""

    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            if not getattr(current_user, flag, False):
                abort(403)
            return fn(*args, **kwargs)

        return wrapper

    return deco