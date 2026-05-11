from __future__ import annotations

from pathlib import Path

import pytest

from kiwi.admin_store import AdminStore


def test_admin_store_login_logout_and_crud(tmp_path: Path) -> None:
    users = tmp_path / "config" / "admins.json"
    sessions = tmp_path / "data" / "sessions.json"
    store = AdminStore(str(users), str(sessions))

    assert store.verify_credentials("admin", "change_me") is True
    assert store.verify_credentials("admin", "wrong") is False

    store.add_admin("root2", "pass2")
    assert "root2" in store.list_admins()

    store.login("100", "root2")
    assert store.is_logged_in("100") is True
    store.logout("100")
    assert store.is_logged_in("100") is False

    store.remove_admin("root2")
    assert "root2" not in store.list_admins()


def test_admin_store_prevents_removing_last_admin(tmp_path: Path) -> None:
    users = tmp_path / "config" / "admins.json"
    sessions = tmp_path / "data" / "sessions.json"
    store = AdminStore(str(users), str(sessions))

    with pytest.raises(ValueError):
        store.remove_admin("admin")


def test_admin_store_repairs_empty_sessions_json(tmp_path: Path) -> None:
    users = tmp_path / "config" / "admins.json"
    sessions = tmp_path / "data" / "sessions.json"
    users.parent.mkdir(parents=True, exist_ok=True)
    sessions.parent.mkdir(parents=True, exist_ok=True)
    users.write_text('[{"username":"admin","password":"change_me"}]', encoding="utf-8")
    sessions.write_text("", encoding="utf-8")

    store = AdminStore(str(users), str(sessions))
    assert store.get_session("777") == {"logged_in": False, "flow": None, "flow_data": {}}
    assert sessions.read_text(encoding="utf-8").strip() == "{}"


def test_admin_store_repairs_invalid_sessions_json(tmp_path: Path) -> None:
    users = tmp_path / "config" / "admins.json"
    sessions = tmp_path / "data" / "sessions.json"
    users.parent.mkdir(parents=True, exist_ok=True)
    sessions.parent.mkdir(parents=True, exist_ok=True)
    users.write_text('[{"username":"admin","password":"change_me"}]', encoding="utf-8")
    sessions.write_text("{", encoding="utf-8")

    store = AdminStore(str(users), str(sessions))
    store.login("100", "admin")
    assert store.is_logged_in("100") is True
