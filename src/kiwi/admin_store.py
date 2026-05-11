from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kiwi.utils import dump_json


class AdminStore:
    def __init__(self, users_config_path: str, sessions_path: str) -> None:
        self.users_path = Path(users_config_path)
        self.sessions_path = Path(sessions_path)
        self.users_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.users_path.exists():
            dump_json(self.users_path, [{"username": "admin", "password": "change_me"}])
        if not self.sessions_path.exists():
            dump_json(self.sessions_path, {})

    def list_admins(self) -> list[str]:
        users = self._load_users()
        return [u["username"] for u in users]

    def verify_credentials(self, username: str, password: str) -> bool:
        username_n = username.strip().lower()
        password_n = password.strip()
        if not username_n or not password_n:
            return False
        for item in self._load_users():
            if item["username"] == username_n and item["password"] == password_n:
                return True
        return False

    def add_admin(self, username: str, password: str) -> None:
        username_n = username.strip().lower()
        password_n = password.strip()
        if not username_n or not password_n:
            raise ValueError("username/password required")
        users = self._load_users()
        if any(u["username"] == username_n for u in users):
            raise ValueError("admin already exists")
        users.append({"username": username_n, "password": password_n})
        dump_json(self.users_path, users)

    def remove_admin(self, username: str) -> None:
        username_n = username.strip().lower()
        users = self._load_users()
        filtered = [u for u in users if u["username"] != username_n]
        if len(filtered) == len(users):
            raise ValueError("admin not found")
        if not filtered:
            raise ValueError("cannot remove last admin")
        dump_json(self.users_path, filtered)
        sessions = self._load_sessions()
        for user_id in list(sessions.keys()):
            info = sessions.get(user_id) or {}
            if str(info.get("username") or "") == username_n:
                sessions.pop(user_id, None)
        dump_json(self.sessions_path, sessions)

    def is_logged_in(self, user_id: str) -> bool:
        sessions = self._load_sessions()
        return str(sessions.get(user_id, {}).get("logged_in") or "").lower() == "true"

    def login(self, user_id: str, username: str) -> None:
        sessions = self._load_sessions()
        sessions[str(user_id)] = {"username": username.strip().lower(), "logged_in": True, "flow": None, "flow_data": {}}
        dump_json(self.sessions_path, sessions)

    def logout(self, user_id: str) -> None:
        sessions = self._load_sessions()
        sessions.pop(str(user_id), None)
        dump_json(self.sessions_path, sessions)

    def get_session(self, user_id: str) -> dict:
        sessions = self._load_sessions()
        sess = sessions.get(str(user_id))
        if not isinstance(sess, dict):
            return {"logged_in": False, "flow": None, "flow_data": {}}
        out = dict(sess)
        if "flow_data" not in out or not isinstance(out.get("flow_data"), dict):
            out["flow_data"] = {}
        return out

    def update_session(self, user_id: str, patch: dict) -> dict:
        sessions = self._load_sessions()
        current = sessions.get(str(user_id))
        if not isinstance(current, dict):
            current = {"logged_in": False, "flow": None, "flow_data": {}}
        merged = dict(current)
        merged.update(patch)
        sessions[str(user_id)] = merged
        dump_json(self.sessions_path, sessions)
        return merged

    def set_flow(self, user_id: str, flow: str | None, flow_data: dict | None = None) -> dict:
        patch = {"flow": flow}
        if flow_data is not None:
            patch["flow_data"] = flow_data
        return self.update_session(user_id, patch)

    def _load_users(self) -> list[dict]:
        raw = self._load_json_file(self.users_path, fallback=[{"username": "admin", "password": "change_me"}], repair=False)
        if not isinstance(raw, list):
            raise ValueError("admin users config must be list")
        users: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username") or "").strip().lower()
            password = str(item.get("password") or "").strip()
            if username and password:
                users.append({"username": username, "password": password})
        if not users:
            raise ValueError("at least one admin user is required")
        return users

    def _load_sessions(self) -> dict:
        raw = self._load_json_file(self.sessions_path, fallback={}, repair=True)
        if not isinstance(raw, dict):
            return {}
        return raw

    def _load_json_file(self, path: Path, *, fallback: Any, repair: bool) -> Any:
        try:
            raw_text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            dump_json(path, fallback)
            return fallback

        if not raw_text.strip():
            if repair:
                dump_json(path, fallback)
                return fallback
            raise ValueError(f"{path} is empty")

        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            if repair:
                dump_json(path, fallback)
                return fallback
            raise ValueError(f"{path} contains invalid JSON") from None
