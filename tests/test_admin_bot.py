from __future__ import annotations

from pathlib import Path

from kiwi.admin_bot import (
    BTN_ADD_ROUTE,
    BTN_EDIT_ENABLED,
    BTN_EDIT_ROUTE,
    BTN_EDIT_SRC_ID,
    BTN_ENABLED_OFF,
    BTN_ENABLED_ON,
    BTN_GUARDS,
    BTN_LOGIN,
    BTN_MAX_DEFAULT,
    BTN_SCRIPTS,
    BTN_SRC_ID,
    BTN_DST_ID,
    AdminBotHandler,
)
from kiwi.admin_store import AdminStore
from kiwi.management_api import ManagementApi
from kiwi.types import AdminInboundMessage


def _inbound(text: str, user_id: str = "100") -> AdminInboundMessage:
    return AdminInboundMessage(
        update_id=1,
        chat_id=user_id,
        user_id=user_id,
        username="@admin",
        text=text,
        raw={},
    )


def _build_bot(tmp_path: Path) -> AdminBotHandler:
    channels_path = tmp_path / "config" / "channels.json"
    channels_path.parent.mkdir(parents=True, exist_ok=True)
    channels_path.write_text("[]", encoding="utf-8")
    scripts_dir = tmp_path / "scripts"
    guards_dir = tmp_path / "guards"
    scripts_dir.mkdir()
    guards_dir.mkdir()
    (scripts_dir / "default_scripts.py").write_text("# x", encoding="utf-8")
    (guards_dir / "default_guard.py").write_text("# x", encoding="utf-8")

    store = AdminStore(str(tmp_path / "config" / "admins.json"), str(tmp_path / "data" / "sessions.json"))
    api = ManagementApi(
        channels_config_path=str(channels_path),
        scripts_dir=str(scripts_dir),
        gaurd_scripts_dir=str(guards_dir),
        on_routes_reloaded=lambda *_: None,
    )
    return AdminBotHandler(admin_store=store, management_api=api)


def _extract_buttons(reply_markup: dict | None) -> set[str]:
    if not isinstance(reply_markup, dict):
        return set()
    out: set[str] = set()
    for row in reply_markup.get("keyboard") or []:
        if not isinstance(row, list):
            continue
        for item in row:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    out.add(text)
    return out


def test_admin_bot_login_and_button_menus(tmp_path: Path) -> None:
    bot = _build_bot(tmp_path)

    prompt = bot.handle(_inbound("/routes"))
    assert "وارد" in prompt.text
    assert BTN_LOGIN in _extract_buttons(prompt.reply_markup)

    login_step = bot.handle(_inbound(BTN_LOGIN))
    assert "نام کاربری" in login_step.text

    pass_step = bot.handle(_inbound("admin"))
    assert "رمز" in pass_step.text

    logged = bot.handle(_inbound("change_me"))
    assert "موفق" in logged.text
    main_buttons = _extract_buttons(logged.reply_markup)
    assert BTN_ADD_ROUTE in main_buttons
    assert BTN_EDIT_ROUTE in main_buttons
    assert BTN_SCRIPTS in main_buttons
    assert BTN_GUARDS in main_buttons


def test_admin_bot_add_and_edit_route_by_buttons(tmp_path: Path) -> None:
    bot = _build_bot(tmp_path)
    bot.handle(_inbound(BTN_LOGIN))
    bot.handle(_inbound("admin"))
    bot.handle(_inbound("change_me"))

    bot.handle(_inbound(BTN_ADD_ROUTE))
    bot.handle(_inbound("r1"))
    bot.handle(_inbound(BTN_SRC_ID))
    bot.handle(_inbound("-1001"))
    bot.handle(_inbound(BTN_DST_ID))
    bot.handle(_inbound("-2001"))
    bot.handle(_inbound("default_scripts.py"))
    bot.handle(_inbound("default_guard.py"))
    bot.handle(_inbound(BTN_MAX_DEFAULT))
    add_result = bot.handle(_inbound(BTN_ENABLED_ON))
    assert "اضافه شد" in add_result.text

    routes = bot.handle(_inbound("/routes"))
    assert "r1" in routes.text
    assert "enabled=True" in routes.text

    bot.handle(_inbound(BTN_EDIT_ROUTE))
    bot.handle(_inbound("r1"))
    bot.handle(_inbound(BTN_EDIT_SRC_ID))
    src_edit = bot.handle(_inbound("-1111"))
    assert "مبدا مسیر ویرایش شد" in src_edit.text

    routes_src = bot.handle(_inbound("/routes"))
    assert "src=-1111" in routes_src.text

    bot.handle(_inbound(BTN_EDIT_ROUTE))
    bot.handle(_inbound("r1"))
    bot.handle(_inbound(BTN_EDIT_ENABLED))
    edit_result = bot.handle(_inbound(BTN_ENABLED_OFF))
    assert "ویرایش شد" in edit_result.text

    routes2 = bot.handle(_inbound("/routes"))
    assert "enabled=False" in routes2.text
