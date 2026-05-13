from __future__ import annotations

import json
import shlex
from dataclasses import dataclass

from kiwi.admin_store import AdminStore
from kiwi.management_api import ManagementApi
from kiwi.types import AdminInboundMessage

BTN_LOGIN = "🔐 ورود"
BTN_LOGOUT = "🚪 خروج"
BTN_BACK = "⬅️ بازگشت"
BTN_CANCEL = "❌ لغو"

BTN_LIST_ROUTES = "📋 لیست مسیرها"
BTN_ADD_ROUTE = "➕ افزودن مسیر"
BTN_EDIT_ROUTE = "✏️ ویرایش مسیر"
BTN_DEL_ROUTE = "🗑 حذف مسیر"
BTN_SCRIPTS = "🧩 لیست اسکریپت‌ها"
BTN_GUARDS = "🛡 لیست گاردها"
BTN_ADMINS = "👤 مدیریت ادمین‌ها"
BTN_RELOAD = "♻️ ریلود مسیرها"

BTN_ADMIN_LIST = "📄 لیست ادمین‌ها"
BTN_ADMIN_ADD = "➕ افزودن ادمین"
BTN_ADMIN_DELETE = "➖ حذف ادمین"

BTN_SRC_ID = "🆔 مبدا با شناسه"
BTN_SRC_USER = "👤 مبدا با یوزرنیم"
BTN_DST_ID = "🆔 مقصد با شناسه"
BTN_DST_USER = "👤 مقصد با یوزرنیم"
BTN_ENABLED_ON = "✅ فعال"
BTN_ENABLED_OFF = "⛔ غیرفعال"
BTN_MAX_DEFAULT = "۵۰"
BTN_MAX_100 = "۱۰۰"
BTN_MAX_200 = "۲۰۰"
BTN_MAX_NONE = "نامحدود"
BTN_NONE = "🚫 بدون اسکریپت"

BTN_EDIT_SCRIPT = "🧩 اسکریپت"
BTN_EDIT_GUARD = "🛡 گارد"
BTN_EDIT_MAX = "📦 حداکثر حجم"
BTN_EDIT_DEST_ID = "🎯 مقصد با شناسه"
BTN_EDIT_DEST_USER = "🎯 مقصد با یوزرنیم"
BTN_EDIT_SRC_ID = "🧭 مبدا با شناسه"
BTN_EDIT_SRC_USER = "🧭 مبدا با یوزرنیم"
BTN_EDIT_ENABLED = "⚙️ وضعیت فعال/غیرفعال"
BTN_EDIT_SYNC_START = "▶️ شروع سینک"
BTN_EDIT_SYNC_STOP = "⏹ توقف سینک"
BTN_EDIT_SYNC_BACKFILL = "🔢 تعداد پیام سینک"
BTN_EDIT_SYNC_INTERVAL = "⏱ فاصله سینک (ثانیه)"
BTN_EDIT_SYNC_BATCH = "📨 تعداد هر نوبت سینک"
BTN_EDIT_SYNC_RETRY = "♻️ تلاش مجدد سینک"
BTN_SYNC_ON = "✅ سینک روشن"
BTN_SYNC_OFF = "⛔ سینک خاموش"


@dataclass(slots=True)
class AdminBotResponse:
    text: str
    reply_markup: dict | None = None
    delete_message_id: int | None = None


class AdminBotHandler:
    def __init__(self, *, admin_store: AdminStore, management_api: ManagementApi) -> None:
        self.admin_store = admin_store
        self.management_api = management_api
        self._callback_value_store: dict[str, str] = {}
        self._callback_seq = 0

    def handle(self, inbound: AdminInboundMessage) -> AdminBotResponse:
        text = self._resolve_inbound_text(inbound)
        normalized_text = self._normalize_user_text(text)

        if text.startswith("__route_edit__:"):
            route_name = text.split(":", 1)[1].strip()
            if not route_name:
                return self._finalize_response(
                    inbound,
                    AdminBotResponse("نام مسیر نامعتبر است.", self._routes_overview_keyboard()),
                    from_flow=False,
                )
            self.management_api.get_route(route_name)
            self.admin_store.set_flow(inbound.user_id, "route_edit_field", {"route_name": route_name})
            return self._finalize_response(
                inbound,
                AdminBotResponse(f"ویرایش مسیر «{route_name}»\nکدام بخش را می‌خواهی تغییر بدهی؟", self._route_edit_fields_keyboard()),
                from_flow=False,
            )

        if text.startswith("__route_del__:"):
            route_name = text.split(":", 1)[1].strip()
            if not route_name:
                return self._finalize_response(
                    inbound,
                    AdminBotResponse("نام مسیر نامعتبر است.", self._routes_overview_keyboard()),
                    from_flow=False,
                )
            self.management_api.delete_route(route_name)
            return self._finalize_response(
                inbound,
                AdminBotResponse(f"مسیر «{route_name}» حذف شد.\n\n{self._routes_text()}", self._routes_overview_keyboard()),
                from_flow=False,
            )

        # Command path is always available.
        if text.startswith("/"):
            return self._finalize_response(inbound, self._handle_command(inbound, text), from_flow=False)

        session = self.admin_store.get_session(inbound.user_id)
        if not bool(session.get("logged_in")):
            flow = str(session.get("flow") or "")
            if flow in {"login_username", "login_password"}:
                if text in {BTN_CANCEL, BTN_BACK}:
                    self.admin_store.set_flow(inbound.user_id, None, {"stage": None})
                    return self._finalize_response(inbound, AdminBotResponse("ورود لغو شد.", self._login_keyboard()), from_flow=True)
                try:
                    return self._finalize_response(inbound, self._handle_flow(inbound, flow, text, session), from_flow=True)
                except Exception as exc:
                    self.admin_store.set_flow(inbound.user_id, None, {"stage": None})
                    return self._finalize_response(
                        inbound, AdminBotResponse(f"خطا در ورود: {exc}", self._login_keyboard()), from_flow=True
                    )
            if text == BTN_LOGIN:
                self.admin_store.set_flow(inbound.user_id, "login_username", {"stage": "username"})
                return self._finalize_response(inbound, AdminBotResponse("نام کاربری را بفرست.", self._login_keyboard()), from_flow=False)
            return self._finalize_response(inbound, AdminBotResponse("برای ورود روی دکمه ورود بزن.", self._login_keyboard()), from_flow=False)

        # Logged in conversational flows.
        flow = str(session.get("flow") or "")
        if flow:
            if text in {BTN_CANCEL, BTN_BACK}:
                self.admin_store.set_flow(inbound.user_id, None, {"stage": None})
                return self._finalize_response(inbound, AdminBotResponse("عملیات لغو شد.", self._main_menu_keyboard()), from_flow=True)
            try:
                return self._finalize_response(inbound, self._handle_flow(inbound, flow, text, session), from_flow=True)
            except Exception as exc:
                self.admin_store.set_flow(inbound.user_id, None, {"stage": None})
                return self._finalize_response(
                    inbound, AdminBotResponse(f"خطا در عملیات: {exc}", self._main_menu_keyboard()), from_flow=True
                )

        # Menu button path.
        if text == BTN_LIST_ROUTES or normalized_text in {"لیست مسیرها", "لیست مسیر ها", "routes", "route list", "list routes"}:
            return self._finalize_response(inbound, AdminBotResponse(self._routes_text(), self._routes_overview_keyboard()), from_flow=False)
        if text == BTN_ADD_ROUTE:
            self.admin_store.set_flow(inbound.user_id, "route_add_name", {"new_route": {}})
            return self._finalize_response(inbound, AdminBotResponse("نام مسیر جدید را بفرست.", self._cancel_keyboard()), from_flow=False)
        if text == BTN_EDIT_ROUTE:
            self.admin_store.set_flow(inbound.user_id, "route_edit_pick", {"stage": "pick"})
            return self._finalize_response(
                inbound,
                AdminBotResponse("نام مسیر برای ویرایش را بزن یا بنویس.", self._route_names_keyboard(include_back=True)),
                from_flow=False,
            )
        if text == BTN_DEL_ROUTE:
            self.admin_store.set_flow(inbound.user_id, "route_delete_pick", {"stage": "pick"})
            return self._finalize_response(
                inbound,
                AdminBotResponse("نام مسیر برای حذف را بزن یا بنویس.", self._route_names_keyboard(include_back=True)),
                from_flow=False,
            )
        if text == BTN_SCRIPTS:
            return self._finalize_response(
                inbound,
                AdminBotResponse(self._list_lines("اسکریپت‌های کانال", self.management_api.list_channel_script_files()), self._main_menu_keyboard()),
                from_flow=False,
            )
        if text == BTN_GUARDS:
            return self._finalize_response(
                inbound,
                AdminBotResponse(self._list_lines("گاردها", self.management_api.list_guard_files()), self._main_menu_keyboard()),
                from_flow=False,
            )
        if text == BTN_RELOAD:
            self.management_api.reload_routes()
            return self._finalize_response(inbound, AdminBotResponse("ریلود مسیرها انجام شد.", self._main_menu_keyboard()), from_flow=False)
        if text == BTN_ADMINS:
            return self._finalize_response(inbound, AdminBotResponse("مدیریت ادمین‌ها", self._admins_menu_keyboard()), from_flow=False)
        if text == BTN_BACK:
            return self._finalize_response(inbound, AdminBotResponse("بازگشت به منوی اصلی.", self._main_menu_keyboard()), from_flow=False)
        if text == BTN_ADMIN_LIST:
            return self._finalize_response(
                inbound,
                AdminBotResponse(self._list_lines("ادمین‌ها", self.admin_store.list_admins()), self._admins_menu_keyboard()),
                from_flow=False,
            )
        if text == BTN_ADMIN_ADD:
            self.admin_store.set_flow(inbound.user_id, "admin_add_username", {"new_admin": {}})
            return self._finalize_response(
                inbound, AdminBotResponse("نام کاربری ادمین جدید را بفرست.", self._cancel_keyboard()), from_flow=False
            )
        if text == BTN_ADMIN_DELETE:
            self.admin_store.set_flow(inbound.user_id, "admin_delete_username", {"stage": "username"})
            return self._finalize_response(
                inbound, AdminBotResponse("نام کاربری ادمینی که باید حذف شود را بفرست.", self._cancel_keyboard()), from_flow=False
            )
        if text == BTN_LOGOUT:
            self.admin_store.logout(inbound.user_id)
            return self._finalize_response(inbound, AdminBotResponse("خروج انجام شد.", self._login_keyboard()), from_flow=False)

        return self._finalize_response(
            inbound,
            AdminBotResponse("گزینه نامعتبر است. از دکمه‌ها استفاده کن.", self._main_menu_keyboard()),
            from_flow=False,
        )

    def _handle_flow(self, inbound: AdminInboundMessage, flow: str, text: str, session: dict) -> AdminBotResponse:
        if flow == "login_username":
            flow_data = dict(session.get("flow_data") or {})
            flow_data["username"] = text.strip()
            self.admin_store.set_flow(inbound.user_id, "login_password", flow_data)
            return AdminBotResponse("رمز عبور را بفرست.", self._login_keyboard())

        if flow == "login_password":
            flow_data = dict(session.get("flow_data") or {})
            username = str(flow_data.get("username") or "")
            if not self.admin_store.verify_credentials(username, text):
                self.admin_store.set_flow(inbound.user_id, None, {})
                return AdminBotResponse("نام کاربری یا رمز اشتباه است.", self._login_keyboard())
            self.admin_store.login(inbound.user_id, username)
            return AdminBotResponse("ورود موفق بود. پنل مدیریت فعال شد.", self._main_menu_keyboard())

        if flow == "admin_add_username":
            flow_data = dict(session.get("flow_data") or {})
            flow_data["username"] = text.strip()
            self.admin_store.set_flow(inbound.user_id, "admin_add_password", flow_data)
            return AdminBotResponse("رمز عبور ادمین جدید را بفرست.", self._cancel_keyboard())

        if flow == "admin_add_password":
            flow_data = dict(session.get("flow_data") or {})
            username = str(flow_data.get("username") or "")
            self.admin_store.add_admin(username, text.strip())
            self.admin_store.set_flow(inbound.user_id, None, {})
            return AdminBotResponse("ادمین اضافه شد.", self._admins_menu_keyboard())

        if flow == "admin_delete_username":
            self.admin_store.remove_admin(text.strip())
            self.admin_store.set_flow(inbound.user_id, None, {})
            return AdminBotResponse("ادمین حذف شد.", self._admins_menu_keyboard())

        if flow == "route_add_name":
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            new_route["name"] = text.strip()
            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_source_type", flow_data)
            return AdminBotResponse("نوع مبدا را انتخاب کن.", self._source_type_keyboard())

        if flow == "route_add_source_type":
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            if text == BTN_SRC_ID:
                new_route["_source_field"] = "source_channel_id"
            elif text == BTN_SRC_USER:
                new_route["_source_field"] = "source_channel_username"
            else:
                return AdminBotResponse("از دکمه‌های نوع مبدا استفاده کن.", self._source_type_keyboard())
            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_source_value", flow_data)
            return AdminBotResponse("مقدار مبدا را بفرست.", self._cancel_keyboard())

        if flow == "route_add_source_value":
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            field = str(new_route.get("_source_field") or "")
            if not field:
                raise ValueError("source field not set")
            new_route[field] = text.strip()
            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_dest_type", flow_data)
            return AdminBotResponse("نوع مقصد را انتخاب کن.", self._dest_type_keyboard())

        if flow == "route_add_dest_type":
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            if text == BTN_DST_ID:
                new_route["_dest_field"] = "destination_channel_id"
            elif text == BTN_DST_USER:
                new_route["_dest_field"] = "destination_channel_username"
            else:
                return AdminBotResponse("از دکمه‌های نوع مقصد استفاده کن.", self._dest_type_keyboard())
            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_dest_value", flow_data)
            return AdminBotResponse("مقدار مقصد را بفرست.", self._cancel_keyboard())

        if flow == "route_add_dest_value":
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            field = str(new_route.get("_dest_field") or "")
            if not field:
                raise ValueError("destination field not set")
            new_route[field] = text.strip()
            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_script", flow_data)
            return AdminBotResponse("اسکریپت کانال را انتخاب کن (یا گزینه بدون اسکریپت).", self._channel_scripts_keyboard(allow_none=True))

        if flow == "route_add_script":
            scripts = self.management_api.list_channel_script_files()
            selected_script = None
            if text != BTN_NONE:
                if text not in scripts:
                    return AdminBotResponse("اسکریپت کانال معتبر انتخاب کن.", self._channel_scripts_keyboard(allow_none=True))
                selected_script = text
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            new_route["channel_script"] = selected_script
            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_guard", flow_data)
            return AdminBotResponse("گارد را انتخاب کن (یا گزینه بدون گارد).", self._guards_keyboard(allow_none=True))

        if flow == "route_add_guard":
            guards = self.management_api.list_guard_files()
            selected_guard = None
            if text != BTN_NONE:
                if text not in guards:
                    return AdminBotResponse("گارد معتبر انتخاب کن.", self._guards_keyboard(allow_none=True))
                selected_guard = text
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            new_route["gaurd_script"] = selected_guard
            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_max", flow_data)
            return AdminBotResponse("حداکثر حجم پیام (MB) را انتخاب/وارد کن.", self._max_keyboard())

        if flow == "route_add_max":
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            parsed = self._normalize_max_input(text)
            if parsed == "__invalid__":
                return AdminBotResponse("عدد معتبر وارد کن یا از دکمه‌ها استفاده کن.", self._max_keyboard())
            new_route["max_message_mb"] = None if parsed is None else max(1, int(parsed))
            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_enabled", flow_data)
            return AdminBotResponse("وضعیت مسیر را انتخاب کن.", self._enabled_keyboard())

        if flow == "route_add_enabled":
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            if text == BTN_ENABLED_ON:
                new_route["enabled"] = True
            elif text == BTN_ENABLED_OFF:
                new_route["enabled"] = False
            else:
                return AdminBotResponse("وضعیت معتبر انتخاب کن.", self._enabled_keyboard())

            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_sync_enabled", flow_data)
            return AdminBotResponse("برای این مسیر سینک زمان‌بندی‌شده می‌خواهی؟", self._sync_toggle_keyboard())

        if flow == "route_add_sync_enabled":
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            if text == BTN_SYNC_OFF:
                new_route["sync"] = {
                    "enabled": False,
                    "status": "active",
                    "backfill_count": 100,
                    "interval_sec": 300,
                    "batch_size": 1,
                    "retry_attempts": 2,
                    "seeded": False,
                }
                return self._finalize_route_add(inbound.user_id, new_route)

            if text != BTN_SYNC_ON:
                return AdminBotResponse("از دکمه‌های سینک استفاده کن.", self._sync_toggle_keyboard())

            new_route["sync"] = {
                "enabled": True,
                "status": "syncing",
                "backfill_count": 100,
                "interval_sec": 300,
                "batch_size": 1,
                "retry_attempts": 2,
                "seeded": False,
            }
            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_sync_backfill", flow_data)
            return AdminBotResponse("چند پیام آخر برای سینک اولیه در نظر گرفته شود؟ (مثلا 100)", self._cancel_keyboard())

        if flow == "route_add_sync_backfill":
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            sync_obj = new_route.get("sync") if isinstance(new_route.get("sync"), dict) else {}
            try:
                sync_obj["backfill_count"] = max(0, int(text.strip()))
            except Exception as exc:
                raise ValueError("عدد معتبر برای backfill وارد کن") from exc
            new_route["sync"] = sync_obj
            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_sync_interval", flow_data)
            return AdminBotResponse("هر چند ثانیه یک بار سینک اجرا شود؟ (مثلا 300)", self._cancel_keyboard())

        if flow == "route_add_sync_interval":
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            sync_obj = new_route.get("sync") if isinstance(new_route.get("sync"), dict) else {}
            try:
                sync_obj["interval_sec"] = max(1, int(text.strip()))
            except Exception as exc:
                raise ValueError("عدد معتبر برای interval وارد کن") from exc
            new_route["sync"] = sync_obj
            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_sync_batch", flow_data)
            return AdminBotResponse("در هر نوبت چند پیام سینک شود؟ (مثلا 1)", self._cancel_keyboard())

        if flow == "route_add_sync_batch":
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            sync_obj = new_route.get("sync") if isinstance(new_route.get("sync"), dict) else {}
            try:
                sync_obj["batch_size"] = max(1, int(text.strip()))
            except Exception as exc:
                raise ValueError("عدد معتبر برای batch size وارد کن") from exc
            new_route["sync"] = sync_obj
            flow_data["new_route"] = new_route
            self.admin_store.set_flow(inbound.user_id, "route_add_sync_retry", flow_data)
            return AdminBotResponse("تعداد تلاش مجدد در خطاهای غیرگارد چقدر باشد؟ (مثلا 2)", self._cancel_keyboard())

        if flow == "route_add_sync_retry":
            flow_data = dict(session.get("flow_data") or {})
            new_route = dict(flow_data.get("new_route") or {})
            sync_obj = new_route.get("sync") if isinstance(new_route.get("sync"), dict) else {}
            try:
                sync_obj["retry_attempts"] = max(0, int(text.strip()))
            except Exception as exc:
                raise ValueError("عدد معتبر برای retry attempts وارد کن") from exc
            new_route["sync"] = sync_obj
            return self._finalize_route_add(inbound.user_id, new_route)

        if flow == "route_delete_pick":
            self.management_api.delete_route(text.strip())
            self.admin_store.set_flow(inbound.user_id, None, {})
            return AdminBotResponse("مسیر حذف شد.", self._main_menu_keyboard())

        if flow == "route_edit_pick":
            route = self.management_api.get_route(text.strip())
            self.admin_store.set_flow(inbound.user_id, "route_edit_field", {"route_name": route.get("name")})
            return AdminBotResponse("کدام بخش مسیر ویرایش شود؟", self._route_edit_fields_keyboard())

        if flow == "route_edit_field":
            flow_data = dict(session.get("flow_data") or {})
            route_name = str(flow_data.get("route_name") or "")
            if not route_name:
                raise ValueError("اطلاعات مسیر برای ویرایش موجود نیست")

            if text == BTN_EDIT_SCRIPT:
                self.admin_store.set_flow(inbound.user_id, "route_edit_script", flow_data)
                return AdminBotResponse("اسکریپت کانال جدید را انتخاب کن.", self._channel_scripts_keyboard(allow_none=True))
            if text == BTN_EDIT_GUARD:
                self.admin_store.set_flow(inbound.user_id, "route_edit_guard", flow_data)
                return AdminBotResponse("گارد جدید را انتخاب کن.", self._guards_keyboard(allow_none=True))
            if text == BTN_EDIT_MAX:
                self.admin_store.set_flow(inbound.user_id, "route_edit_max", flow_data)
                return AdminBotResponse("حداکثر حجم جدید (MB) را انتخاب/وارد کن.", self._max_keyboard())
            if text == BTN_EDIT_ENABLED:
                self.admin_store.set_flow(inbound.user_id, "route_edit_enabled", flow_data)
                return AdminBotResponse("وضعیت جدید را انتخاب کن.", self._enabled_keyboard())
            if text == BTN_EDIT_DEST_ID:
                flow_data["field"] = "destination_channel_id"
                self.admin_store.set_flow(inbound.user_id, "route_edit_dest_value", flow_data)
                return AdminBotResponse("شناسه مقصد جدید را بفرست.", self._cancel_keyboard())
            if text == BTN_EDIT_DEST_USER:
                flow_data["field"] = "destination_channel_username"
                self.admin_store.set_flow(inbound.user_id, "route_edit_dest_value", flow_data)
                return AdminBotResponse("یوزرنیم مقصد جدید را بفرست (مثل @my_channel).", self._cancel_keyboard())
            if text == BTN_EDIT_SRC_ID:
                flow_data["field"] = "source_channel_id"
                self.admin_store.set_flow(inbound.user_id, "route_edit_source_value", flow_data)
                return AdminBotResponse("شناسه مبدا جدید را بفرست.", self._cancel_keyboard())
            if text == BTN_EDIT_SRC_USER:
                flow_data["field"] = "source_channel_username"
                self.admin_store.set_flow(inbound.user_id, "route_edit_source_value", flow_data)
                return AdminBotResponse("یوزرنیم مبدا جدید را بفرست (مثل @my_channel).", self._cancel_keyboard())
            if text == BTN_EDIT_SYNC_START:
                updated = self.management_api.start_route_sync(route_name)
                self.admin_store.set_flow(inbound.user_id, None, {})
                sync = updated.get("sync") if isinstance(updated.get("sync"), dict) else {}
                return AdminBotResponse(
                    f"سینک مسیر شروع شد. status={sync.get('status') or 'syncing'}",
                    self._main_menu_keyboard(),
                )
            if text == BTN_EDIT_SYNC_STOP:
                self.management_api.stop_route_sync(route_name)
                self.admin_store.set_flow(inbound.user_id, None, {})
                return AdminBotResponse("سینک مسیر متوقف شد.", self._main_menu_keyboard())
            if text == BTN_EDIT_SYNC_BACKFILL:
                flow_data["field"] = "sync_backfill_count"
                self.admin_store.set_flow(inbound.user_id, "route_edit_sync_number", flow_data)
                return AdminBotResponse("تعداد پیام سینک اولیه را وارد کن (مثلا 100).", self._cancel_keyboard())
            if text == BTN_EDIT_SYNC_INTERVAL:
                flow_data["field"] = "sync_interval_sec"
                self.admin_store.set_flow(inbound.user_id, "route_edit_sync_number", flow_data)
                return AdminBotResponse("فاصله زمانی سینک را بر حسب ثانیه وارد کن (مثلا 300).", self._cancel_keyboard())
            if text == BTN_EDIT_SYNC_BATCH:
                flow_data["field"] = "sync_batch_size"
                self.admin_store.set_flow(inbound.user_id, "route_edit_sync_number", flow_data)
                return AdminBotResponse("در هر نوبت چند پیام سینک شود؟ (مثلا 1)", self._cancel_keyboard())
            if text == BTN_EDIT_SYNC_RETRY:
                flow_data["field"] = "sync_retry_attempts"
                self.admin_store.set_flow(inbound.user_id, "route_edit_sync_number", flow_data)
                return AdminBotResponse("تعداد تلاش مجدد برای خطاهای غیرگارد را وارد کن (مثلا 2).", self._cancel_keyboard())
            return AdminBotResponse("از دکمه‌های ویرایش استفاده کن.", self._route_edit_fields_keyboard())

        if flow == "route_edit_script":
            flow_data = dict(session.get("flow_data") or {})
            route_name = str(flow_data.get("route_name") or "")
            if not route_name:
                raise ValueError("اطلاعات ویرایش ناقص است")
            scripts = self.management_api.list_channel_script_files()
            value = None
            if text != BTN_NONE:
                if text not in scripts:
                    return AdminBotResponse("اسکریپت کانال معتبر انتخاب کن.", self._channel_scripts_keyboard(allow_none=True))
                value = text
            self.management_api.update_route(route_name, {"channel_script": value})
            self.admin_store.set_flow(inbound.user_id, None, {})
            return AdminBotResponse("اسکریپت کانال مسیر ویرایش شد.", self._main_menu_keyboard())

        if flow == "route_edit_guard":
            flow_data = dict(session.get("flow_data") or {})
            route_name = str(flow_data.get("route_name") or "")
            if not route_name:
                raise ValueError("اطلاعات ویرایش ناقص است")
            guards = self.management_api.list_guard_files()
            value = None
            if text != BTN_NONE:
                if text not in guards:
                    return AdminBotResponse("گارد معتبر انتخاب کن.", self._guards_keyboard(allow_none=True))
                value = text
            self.management_api.update_route(route_name, {"gaurd_script": value})
            self.admin_store.set_flow(inbound.user_id, None, {})
            return AdminBotResponse("گارد مسیر ویرایش شد.", self._main_menu_keyboard())

        if flow == "route_edit_max":
            flow_data = dict(session.get("flow_data") or {})
            route_name = str(flow_data.get("route_name") or "")
            if not route_name:
                raise ValueError("اطلاعات ویرایش ناقص است")
            parsed = self._normalize_max_input(text)
            if parsed == "__invalid__":
                return AdminBotResponse("عدد معتبر وارد کن یا از دکمه‌ها استفاده کن.", self._max_keyboard())
            value = None if parsed is None else max(1, int(parsed))
            self.management_api.update_route(route_name, {"max_message_mb": value})
            self.admin_store.set_flow(inbound.user_id, None, {})
            return AdminBotResponse("حداکثر حجم مسیر ویرایش شد.", self._main_menu_keyboard())

        if flow == "route_edit_enabled":
            flow_data = dict(session.get("flow_data") or {})
            route_name = str(flow_data.get("route_name") or "")
            if not route_name:
                raise ValueError("اطلاعات ویرایش ناقص است")
            if text == BTN_ENABLED_ON:
                value = True
            elif text == BTN_ENABLED_OFF:
                value = False
            else:
                return AdminBotResponse("وضعیت معتبر انتخاب کن.", self._enabled_keyboard())
            self.management_api.update_route(route_name, {"enabled": value})
            self.admin_store.set_flow(inbound.user_id, None, {})
            return AdminBotResponse("وضعیت مسیر ویرایش شد.", self._main_menu_keyboard())

        if flow == "route_edit_dest_value":
            flow_data = dict(session.get("flow_data") or {})
            route_name = str(flow_data.get("route_name") or "")
            field = str(flow_data.get("field") or "")
            if not route_name or field not in {"destination_channel_id", "destination_channel_username"}:
                raise ValueError("اطلاعات ویرایش مقصد ناقص است")
            self.management_api.update_route(route_name, {field: text.strip()})
            self.admin_store.set_flow(inbound.user_id, None, {})
            return AdminBotResponse("مقصد مسیر ویرایش شد.", self._main_menu_keyboard())

        if flow == "route_edit_source_value":
            flow_data = dict(session.get("flow_data") or {})
            route_name = str(flow_data.get("route_name") or "")
            field = str(flow_data.get("field") or "")
            if not route_name or field not in {"source_channel_id", "source_channel_username"}:
                raise ValueError("اطلاعات ویرایش مبدا ناقص است")
            self.management_api.update_route(route_name, {field: text.strip()})
            self.admin_store.set_flow(inbound.user_id, None, {})
            return AdminBotResponse("مبدا مسیر ویرایش شد.", self._main_menu_keyboard())

        if flow == "route_edit_sync_number":
            flow_data = dict(session.get("flow_data") or {})
            route_name = str(flow_data.get("route_name") or "")
            field = str(flow_data.get("field") or "")
            if not route_name:
                raise ValueError("نام مسیر برای ویرایش سینک مشخص نیست")
            try:
                value = int(text.strip())
            except Exception as exc:
                raise ValueError("عدد معتبر وارد کن") from exc

            route = self.management_api.get_route(route_name)
            sync_obj = route.get("sync") if isinstance(route.get("sync"), dict) else {}
            if field == "sync_backfill_count":
                sync_obj["backfill_count"] = max(0, value)
                sync_obj["seeded"] = False
            elif field == "sync_interval_sec":
                sync_obj["interval_sec"] = max(1, value)
            elif field == "sync_batch_size":
                sync_obj["batch_size"] = max(1, value)
            elif field == "sync_retry_attempts":
                sync_obj["retry_attempts"] = max(0, value)
            else:
                raise ValueError("فیلد سینک نامعتبر است")

            self.management_api.update_route(route_name, {"sync": sync_obj})
            self.admin_store.set_flow(inbound.user_id, None, {})
            return AdminBotResponse("تنظیمات سینک مسیر ذخیره شد.", self._main_menu_keyboard())

        self.admin_store.set_flow(inbound.user_id, None, {})
        return AdminBotResponse("حالت گفتگو نامعتبر بود. بازگشت به منوی اصلی.", self._main_menu_keyboard())

    def _finalize_route_add(self, user_id: str, new_route: dict) -> AdminBotResponse:
        new_route.pop("_source_field", None)
        new_route.pop("_dest_field", None)
        if not isinstance(new_route.get("sync"), dict):
            new_route["sync"] = {
                "enabled": False,
                "status": "active",
                "backfill_count": 100,
                "interval_sec": 300,
                "batch_size": 1,
                "retry_attempts": 2,
                "seeded": False,
            }
        self.management_api.add_route(new_route)
        self.admin_store.set_flow(user_id, None, {})
        sync_obj = new_route.get("sync") if isinstance(new_route.get("sync"), dict) else {}
        if bool(sync_obj.get("enabled", False)):
            return AdminBotResponse(
                f"مسیر «{new_route.get('name')}» اضافه شد و سینک روی حالت syncing فعال شد.",
                self._main_menu_keyboard(),
            )
        return AdminBotResponse(f"مسیر «{new_route.get('name')}» اضافه شد و فعال گردید.", self._main_menu_keyboard())

    def _handle_command(self, inbound: AdminInboundMessage, text: str) -> AdminBotResponse:
        if text.startswith("/start"):
            session = self.admin_store.get_session(inbound.user_id)
            if bool(session.get("logged_in")):
                return AdminBotResponse("پنل مدیریت آماده است.", self._main_menu_keyboard())
            return AdminBotResponse("خوش آمدی. برای ورود روی دکمه ورود بزن.", self._login_keyboard())

        if text.startswith("/help"):
            return AdminBotResponse(self._help_text(), self._main_menu_keyboard() if self.admin_store.is_logged_in(inbound.user_id) else self._login_keyboard())

        if text.startswith("/login"):
            parts = shlex.split(text)
            if len(parts) != 3:
                self.admin_store.set_flow(inbound.user_id, "login_username", {"stage": "username"})
                return AdminBotResponse("فرمت درست: /login <username> <password>\nیا از حالت مرحله‌ای استفاده کن.", self._login_keyboard())
            username, password = parts[1], parts[2]
            if not self.admin_store.verify_credentials(username, password):
                return AdminBotResponse("نام کاربری یا رمز اشتباه است.", self._login_keyboard())
            self.admin_store.login(inbound.user_id, username=username)
            return AdminBotResponse("ورود موفق بود.", self._main_menu_keyboard())

        if text.startswith("/logout"):
            self.admin_store.logout(inbound.user_id)
            return AdminBotResponse("خروج انجام شد.", self._login_keyboard())

        if not self.admin_store.is_logged_in(inbound.user_id):
            return AdminBotResponse("برای مدیریت ابتدا وارد شو. /login یا دکمه ورود", self._login_keyboard())

        # Legacy command compatibility for power users.
        if text.startswith("/routes"):
            return AdminBotResponse(self._routes_text(), self._routes_overview_keyboard())
        if text.startswith("/scripts"):
            return AdminBotResponse(self._list_lines("اسکریپت‌های کانال", self.management_api.list_channel_script_files()), self._main_menu_keyboard())
        if text.startswith("/guards"):
            return AdminBotResponse(self._list_lines("گاردها", self.management_api.list_guard_files()), self._main_menu_keyboard())
        if text.startswith("/admins"):
            return AdminBotResponse(self._list_lines("ادمین‌ها", self.admin_store.list_admins()), self._main_menu_keyboard())
        if text.startswith("/admin_add"):
            parts = shlex.split(text)
            if len(parts) != 3:
                return AdminBotResponse("فرمت درست:\n/admin_add <username> <password>", self._main_menu_keyboard())
            self.admin_store.add_admin(parts[1], parts[2])
            return AdminBotResponse("ادمین اضافه شد.", self._main_menu_keyboard())
        if text.startswith("/admin_delete"):
            parts = shlex.split(text)
            if len(parts) != 2:
                return AdminBotResponse("فرمت درست:\n/admin_delete <username>", self._main_menu_keyboard())
            self.admin_store.remove_admin(parts[1])
            return AdminBotResponse("ادمین حذف شد.", self._main_menu_keyboard())
        if text.startswith("/route_add"):
            payload = self._json_tail(text, "/route_add")
            route = self.management_api.add_route(payload)
            return AdminBotResponse(f"مسیر اضافه شد: {route.get('name')}", self._main_menu_keyboard())
        if text.startswith("/route_update"):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                return AdminBotResponse("فرمت درست:\n/route_update <route_name> <json_patch>", self._main_menu_keyboard())
            name = parts[1].strip()
            patch = self._parse_json(parts[2].strip())
            route = self.management_api.update_route(name, patch)
            return AdminBotResponse(f"مسیر به‌روزرسانی شد: {route.get('name')}", self._main_menu_keyboard())
        if text.startswith("/route_delete"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                return AdminBotResponse("فرمت درست:\n/route_delete <route_name>", self._main_menu_keyboard())
            self.management_api.delete_route(parts[1].strip())
            return AdminBotResponse("مسیر حذف شد.", self._main_menu_keyboard())
        if text.startswith("/route_enable"):
            parts = text.split(maxsplit=2)
            if len(parts) != 3:
                return AdminBotResponse("فرمت درست:\n/route_enable <route_name> <true|false>", self._main_menu_keyboard())
            enabled = parts[2].strip().lower() in {"1", "true", "yes", "on"}
            route = self.management_api.set_route_enabled(parts[1].strip(), enabled)
            return AdminBotResponse(f"وضعیت مسیر {route.get('name')} -> {enabled}", self._main_menu_keyboard())
        if text.startswith("/reload_routes"):
            self.management_api.reload_routes()
            return AdminBotResponse("ریلود شد.", self._main_menu_keyboard())
        if text.startswith("/sync_stats"):
            stats = self.management_api.sync_stats_snapshot()
            lines = [
                "SYNC stats:",
                f"queue_depth: {stats.get('queue_depth') if stats.get('queue_depth') is not None else 'n/a'}",
                f"open_reviews: {int(stats.get('open_reviews') or 0)}",
                f"status_counts: {json.dumps(stats.get('status_counts') or {}, ensure_ascii=False)}",
            ]
            return AdminBotResponse("\n".join(lines), self._main_menu_keyboard())
        if text.startswith("/sync_review_retry"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                return AdminBotResponse("فرمت درست:\n/sync_review_retry <review_id>", self._main_menu_keyboard())
            result = self.management_api.sync_review_retry(int(parts[1].strip()))
            return AdminBotResponse(f"review retry شد: {result.get('id')}", self._main_menu_keyboard())
        if text.startswith("/sync_review_skip"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                return AdminBotResponse("فرمت درست:\n/sync_review_skip <review_id>", self._main_menu_keyboard())
            result = self.management_api.sync_review_skip(int(parts[1].strip()))
            return AdminBotResponse(f"review skip شد: {result.get('id')}", self._main_menu_keyboard())
        if text.startswith("/sync_review"):
            parts = text.split()
            only_open = True
            limit = 20
            if len(parts) >= 2:
                only_open = parts[1].strip().lower() != "all"
            if len(parts) >= 3:
                limit = max(1, int(parts[2]))
            items = self.management_api.sync_review_list(limit=limit, only_open=only_open)
            if not items:
                return AdminBotResponse("review queue خالی است.", self._main_menu_keyboard())
            lines = ["SYNC review queue:"]
            for item in items:
                lines.append(
                    f"#{item.get('id')} | route={item.get('route_name')} | reason={item.get('reason')} | "
                    f"resolved={item.get('resolved_at') or '-'}"
                )
            return AdminBotResponse("\n".join(lines), self._main_menu_keyboard())

        return AdminBotResponse("دستور نامعتبر است. /help", self._main_menu_keyboard())

    def _routes_text(self) -> str:
        routes = self.management_api.list_routes()
        if not routes:
            return "هیچ مسیری ثبت نشده.\nبرای شروع روی «➕ افزودن مسیر» بزن."
        lines = ["📋 مسیرهای ثبت‌شده:"]
        for idx, r in enumerate(routes, start=1):
            sync_obj = r.get("sync") if isinstance(r.get("sync"), dict) else {}
            sync_enabled = bool(sync_obj.get("enabled", False))
            sync_status = str(sync_obj.get("status", "active"))
            sync_pending = 0
            if self.management_api.sync_ledger is not None:
                sync_pending = self.management_api.sync_ledger.active_count_for_route(str(r.get("name") or ""))
            enabled_icon = "✅" if bool(r.get("enabled", True)) else "⛔"
            max_mb_text = str(r.get("max_message_mb")) if r.get("max_message_mb") is not None else "نامحدود"
            lines.append(f"{idx}) {enabled_icon} {r.get('name', '-')}")
            lines.append(f"مبدا: {r.get('source_channel_username') or r.get('source_channel_id')}")
            lines.append(f"مقصد: {r.get('destination_channel_username') or r.get('destination_channel_id')}")
            lines.append(f"اسکریپت کانال: {r.get('channel_script') or 'ندارد'}")
            lines.append(f"گارد: {r.get('gaurd_script') or 'ندارد'}")
            lines.append(f"حداکثر حجم: {max_mb_text} MB")
            lines.append(f"سینک: {sync_status if sync_enabled else 'off'} | pending={sync_pending}")
            lines.append("────────")
        return "\n".join(lines)

    @staticmethod
    def _help_text() -> str:
        return (
            "راهنما:\n"
            "1) روی «🔐 ورود» بزن و لاگین کن.\n"
            "2) از منوی دکمه‌ای مسیرها/ادمین‌ها را مدیریت کن.\n"
            "3) /logout برای خروج.\n\n"
            "دستورات پیشرفته:\n"
            "/routes /scripts /guards /admins\n"
            "/route_add <json>\n"
            "/route_update <name> <json_patch>\n"
            "/route_delete <name>\n"
            "/route_enable <name> <true|false>\n"
            "/reload_routes\n"
            "/sync_stats\n"
            "/sync_review [open|all] [limit]\n"
            "/sync_review_retry <review_id>\n"
            "/sync_review_skip <review_id>"
        )

    @staticmethod
    def _list_lines(title: str, items: list[str]) -> str:
        if not items:
            return f"{title}: خالی"
        lines = [f"{title}:"]
        for idx, item in enumerate(items, start=1):
            lines.append(f"{idx}. {item}")
        return "\n".join(lines)

    @staticmethod
    def _coerce_field_value(field: str, value: str):
        val = value.strip()
        if field == "enabled":
            return val.lower() in {"1", "true", "yes", "on", "فعال", "✅"}
        if field == "max_message_mb":
            if val in {"", "none", "null", "نامحدود", "-"}:
                return None
            return max(1, int(val))
        return val

    @staticmethod
    def _normalize_max_input(value: str):
        val = value.strip()
        if val == BTN_MAX_NONE or val in {"", "none", "null", "نامحدود", "-"}:
            return None
        digit_map = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
        normalized = val.translate(digit_map)
        try:
            return int(normalized)
        except Exception:
            return "__invalid__"

    @staticmethod
    def _json_tail(text: str, cmd: str) -> dict:
        tail = text[len(cmd) :].strip()
        if not tail:
            raise ValueError("JSON payload is required")
        return AdminBotHandler._parse_json(tail)

    @staticmethod
    def _parse_json(value: str) -> dict:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("JSON باید آبجکت باشد")
        return parsed

    @staticmethod
    def _normalize_user_text(value: str) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        return " ".join(text.split())

    def _resolve_inbound_text(self, inbound: AdminInboundMessage) -> str:
        text = (inbound.text or "").strip()
        if text:
            return text
        callback_data = (inbound.callback_data or "").strip()
        if not callback_data:
            return ""
        return self._decode_callback_data(callback_data)

    def _finalize_response(self, inbound: AdminInboundMessage, response: AdminBotResponse, *, from_flow: bool) -> AdminBotResponse:
        delete_message_id = response.delete_message_id
        if delete_message_id is None:
            if inbound.callback_message_id and inbound.callback_message_id > 0:
                delete_message_id = inbound.callback_message_id
            elif from_flow and inbound.message_id and inbound.message_id > 0:
                delete_message_id = inbound.message_id
        return AdminBotResponse(
            text=response.text,
            reply_markup=response.reply_markup,
            delete_message_id=delete_message_id,
        )

    def _encode_callback_data(self, value: str) -> str:
        payload = str(value or "").strip()
        encoded = f"txt:{payload}"
        if len(encoded.encode("utf-8")) <= 64:
            return encoded
        self._callback_seq += 1
        ref = f"r{self._callback_seq}"
        self._callback_value_store[ref] = payload
        return f"ref:{ref}"

    def _decode_callback_data(self, data: str) -> str:
        raw = str(data or "").strip()
        if raw.startswith("txt:"):
            return raw[4:].strip()
        if raw.startswith("ref:"):
            ref = raw[4:].strip()
            return str(self._callback_value_store.get(ref) or "")
        return raw

    def _inline_keyboard(self, rows: list[list[str | tuple[str, str]]]) -> dict:
        inline_rows: list[list[dict[str, str]]] = []
        for row in rows:
            inline_row: list[dict[str, str]] = []
            for item in row:
                if isinstance(item, tuple):
                    text, payload = item[0], item[1]
                else:
                    text, payload = item, item
                inline_row.append({"text": text, "callback_data": self._encode_callback_data(payload)})
            if inline_row:
                inline_rows.append(inline_row)
        return {"inline_keyboard": inline_rows}

    def _login_keyboard(self) -> dict:
        return self._inline_keyboard([[BTN_LOGIN]])

    def _main_menu_keyboard(self) -> dict:
        return self._inline_keyboard(
            [
                [BTN_LIST_ROUTES, BTN_ADD_ROUTE],
                [BTN_EDIT_ROUTE, BTN_DEL_ROUTE],
                [BTN_SCRIPTS],
                [BTN_GUARDS],
                [BTN_ADMINS, BTN_RELOAD],
                [BTN_LOGOUT],
            ]
        )

    def _admins_menu_keyboard(self) -> dict:
        return self._inline_keyboard(
            [
                [BTN_ADMIN_LIST, BTN_ADMIN_ADD],
                [BTN_ADMIN_DELETE, BTN_BACK],
            ]
        )

    def _source_type_keyboard(self) -> dict:
        return self._inline_keyboard([[BTN_SRC_ID, BTN_SRC_USER], [BTN_CANCEL]])

    def _dest_type_keyboard(self) -> dict:
        return self._inline_keyboard([[BTN_DST_ID, BTN_DST_USER], [BTN_CANCEL]])

    def _channel_scripts_keyboard(self, *, allow_none: bool = False) -> dict:
        items = self.management_api.list_channel_script_files()
        rows = [[name] for name in items]
        if allow_none:
            rows.append([BTN_NONE])
        rows.append([BTN_CANCEL])
        return self._inline_keyboard(rows)

    def _scripts_keyboard(self) -> dict:
        # Backward compatibility with existing tests/helpers.
        return self._channel_scripts_keyboard()

    def _guards_keyboard(self, *, allow_none: bool = False) -> dict:
        items = self.management_api.list_guard_files()
        rows = [[name] for name in items]
        if allow_none:
            rows.append([BTN_NONE])
        rows.append([BTN_CANCEL])
        return self._inline_keyboard(rows)

    def _max_keyboard(self) -> dict:
        return self._inline_keyboard([[BTN_MAX_DEFAULT, BTN_MAX_100, BTN_MAX_200], [BTN_MAX_NONE], [BTN_CANCEL]])

    def _enabled_keyboard(self) -> dict:
        return self._inline_keyboard([[BTN_ENABLED_ON, BTN_ENABLED_OFF], [BTN_CANCEL]])

    def _sync_toggle_keyboard(self) -> dict:
        return self._inline_keyboard([[BTN_SYNC_ON, BTN_SYNC_OFF], [BTN_CANCEL]])

    def _route_names_keyboard(self, *, include_back: bool) -> dict:
        names = [str(item.get("name") or "").strip() for item in self.management_api.list_routes()]
        names = [n for n in names if n]
        rows = [[name] for name in names]
        controls = [BTN_CANCEL]
        if include_back:
            controls = [BTN_BACK, BTN_CANCEL]
        rows.append(controls)
        return self._inline_keyboard(rows)

    def _routes_overview_keyboard(self) -> dict:
        routes = self.management_api.list_routes()
        rows: list[list[str | tuple[str, str]]] = []
        for item in routes:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            rows.append(
                [
                    (f"✏️ ویرایش {name}", f"__route_edit__:{name}"),
                    (f"🗑 حذف {name}", f"__route_del__:{name}"),
                ]
            )
        rows.append([BTN_ADD_ROUTE, BTN_BACK])
        rows.append([BTN_RELOAD])
        return self._inline_keyboard(rows)

    def _route_edit_fields_keyboard(self) -> dict:
        return self._inline_keyboard(
            [
                [BTN_EDIT_SCRIPT],
                [BTN_EDIT_GUARD],
                [BTN_EDIT_MAX, BTN_EDIT_ENABLED],
                [BTN_EDIT_SRC_ID],
                [BTN_EDIT_SRC_USER],
                [BTN_EDIT_DEST_ID],
                [BTN_EDIT_DEST_USER],
                [BTN_EDIT_SYNC_START, BTN_EDIT_SYNC_STOP],
                [BTN_EDIT_SYNC_BACKFILL],
                [BTN_EDIT_SYNC_INTERVAL],
                [BTN_EDIT_SYNC_BATCH],
                [BTN_EDIT_SYNC_RETRY],
                [BTN_BACK, BTN_CANCEL],
            ]
        )

    def _cancel_keyboard(self) -> dict:
        return self._inline_keyboard([[BTN_CANCEL]])
