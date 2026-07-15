"""User storage backed by a JSON file. Atomic writes via temp-file + rename.

For small-to-medium deployments (≤ a few hundred users). Swap with SQLite/Postgres
when you outgrow it.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from passlib.context import CryptContext

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
_lock = threading.RLock()


class UserStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {"users": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"users": []}

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def list_users(self) -> list[dict]:
        return self._load()["users"]

    def get_by_username(self, username: str) -> Optional[dict]:
        username = (username or "").strip().lower()
        for u in self._load()["users"]:
            if u["username"].lower() == username:
                return u
        return None

    def get_by_email(self, email: str) -> Optional[dict]:
        email = (email or "").strip().lower()
        if not email:
            return None
        for u in self._load()["users"]:
            if u.get("email", "").lower() == email:
                return u
        return None

    def verify_password(self, username: str, password: str) -> Optional[dict]:
        user = self.get_by_username(username)
        if not user or not user.get("password_hash"):
            return None
        if not _pwd.verify(password, user["password_hash"]):
            return None
        return user

    def create_local_user(self, username: str, password: str, email: str = "", role: str = "user") -> dict:
        username = username.strip()
        if not username or not password:
            raise ValueError("username and password required")
        with _lock:
            data = self._load()
            if any(u["username"].lower() == username.lower() for u in data["users"]):
                raise ValueError(f"user '{username}' already exists")
            user = {
                "id": secrets.token_urlsafe(8),
                "username": username,
                "email": email.strip(),
                "role": role,
                "auth_source": "local",
                "password_hash": _pwd.hash(password),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            data["users"].append(user)
            self._save(data)
            return user

    def upsert_oidc_user(self, *, sub: str, email: str, name: str, preferred_username: str) -> dict:
        """Create user from OIDC claims, or return existing one matched by sub/email."""
        with _lock:
            data = self._load()
            # Try match by OIDC sub first
            for u in data["users"]:
                if u.get("oidc_sub") == sub:
                    return u
            # Fallback: match by email
            if email:
                for u in data["users"]:
                    if u.get("email", "").lower() == email.lower():
                        u["oidc_sub"] = sub
                        self._save(data)
                        return u
            # Create new
            username = preferred_username or email.split("@")[0]
            base = username
            i = 1
            existing = {u["username"].lower() for u in data["users"]}
            while username.lower() in existing:
                i += 1
                username = f"{base}{i}"
            user = {
                "id": secrets.token_urlsafe(8),
                "username": username,
                "email": email,
                "display_name": name,
                "role": "user",
                "auth_source": "oidc",
                "oidc_sub": sub,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            data["users"].append(user)
            self._save(data)
            return user

    def update_last_login(self, user_id: str) -> None:
        with _lock:
            data = self._load()
            for u in data["users"]:
                if u["id"] == user_id:
                    u["last_login_at"] = datetime.now(timezone.utc).isoformat()
                    self._save(data)
                    return
