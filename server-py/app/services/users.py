from __future__ import annotations

from ..config import uid
from ..db import execute, now_iso, one
from ..security import hash_password
from .providers import seed_demo_providers


def public_user(u: dict) -> dict:
    return {
        "id": u["id"],
        "username": u["username"],
        "displayName": u.get("display_name"),
        "createdAt": u["created_at"],
    }


def create_user(username: str, password: str, display_name: str | None = None) -> dict:
    user_id = uid("usr_")
    execute(
        """INSERT INTO users (id, username, password_hash, display_name, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, username, hash_password(password), display_name or username, now_iso()),
    )
    seed_demo_providers(user_id)
    row = one("SELECT * FROM users WHERE id = ?", (user_id,))
    assert row is not None
    return row
