from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from kiwi.config import RouteRegistry, load_routes
from kiwi.utils import dump_json


class ManagementApi:
    def __init__(
        self,
        *,
        channels_config_path: str,
        scripts_dir: str,
        gaurd_scripts_dir: str,
        on_routes_reloaded: Callable[[RouteRegistry], None],
    ) -> None:
        self.channels_path = Path(channels_config_path)
        self.scripts_dir = Path(scripts_dir)
        self.guards_dir = Path(gaurd_scripts_dir)
        self.on_routes_reloaded = on_routes_reloaded
        self.channels_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.channels_path.exists():
            dump_json(self.channels_path, [])

    def list_routes(self) -> list[dict]:
        return self._load_routes_raw()

    def add_route(self, route_obj: dict) -> dict:
        if not isinstance(route_obj, dict):
            raise ValueError("route payload must be object")
        routes = self._load_routes_raw()
        name = str(route_obj.get("name") or "").strip()
        if not name:
            raise ValueError("route name is required")
        if any(str(item.get("name") or "").strip() == name for item in routes):
            raise ValueError("route name already exists")
        routes.append(route_obj)
        self._save_and_reload(routes)
        return route_obj

    def update_route(self, name: str, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ValueError("patch must be object")
        routes = self._load_routes_raw()
        for idx, item in enumerate(routes):
            if str(item.get("name") or "").strip() != name:
                continue
            updated = dict(item)
            updated.update(patch)
            if "name" in patch and str(patch.get("name") or "").strip() != name:
                raise ValueError("renaming routes is not supported")
            routes[idx] = updated
            self._save_and_reload(routes)
            return updated
        raise ValueError("route not found")

    def get_route(self, name: str) -> dict:
        for item in self._load_routes_raw():
            if str(item.get("name") or "").strip() == name:
                return item
        raise ValueError("route not found")

    def delete_route(self, name: str) -> None:
        routes = self._load_routes_raw()
        filtered = [item for item in routes if str(item.get("name") or "").strip() != name]
        if len(filtered) == len(routes):
            raise ValueError("route not found")
        self._save_and_reload(filtered)

    def set_route_enabled(self, name: str, enabled: bool) -> dict:
        return self.update_route(name, {"enabled": bool(enabled)})

    def list_script_files(self) -> list[str]:
        return sorted(p.name for p in self.scripts_dir.glob("*.py") if p.is_file())

    def list_guard_files(self) -> list[str]:
        return sorted(p.name for p in self.guards_dir.glob("*.py") if p.is_file())

    def reload_routes(self) -> None:
        registry = load_routes(str(self.channels_path))
        self.on_routes_reloaded(registry)

    def _save_and_reload(self, routes: list[dict]) -> None:
        dump_json(self.channels_path, routes)
        self.reload_routes()

    def _load_routes_raw(self) -> list[dict]:
        raw = json.loads(self.channels_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("channels config must be list")
        out: list[dict] = []
        for item in raw:
            if isinstance(item, dict):
                out.append(item)
        return out
