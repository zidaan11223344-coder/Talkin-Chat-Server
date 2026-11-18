"""Persistent per-room member welcome settings and custom templates."""
from __future__ import annotations

from pathlib import Path

from state import JsonState, normalize

DEFAULT_TEMPLATE = "أهلًا {username} في غرفة {room}"
MAX_TEMPLATE_BYTES = 700


def _blank():
    return {"rooms": {}}


class WelcomeTemplateStore:
    def __init__(self, root: str | Path):
        path = Path(root)
        if path.suffix.lower() != ".json":
            path = path / "welcome_templates.json"
        self.state = JsonState(path, _blank)

    def _room(self, room: str) -> dict:
        data = self.state.load()
        rooms = data.get("rooms", {}) if isinstance(data, dict) else {}
        value = rooms.get(normalize(room), {}) if isinstance(rooms, dict) else {}
        return dict(value) if isinstance(value, dict) else {}

    def set_enabled(self, room: str, enabled: bool) -> None:
        key = normalize(room)
        if not key:
            raise ValueError("اسم الغرفة مطلوب")
        self.state.mutate(lambda data: data.setdefault("rooms", {}).setdefault(key, {}).update({"enabled": bool(enabled)}))

    def set_custom_enabled(self, room: str, enabled: bool) -> None:
        key = normalize(room)
        if not key:
            raise ValueError("اسم الغرفة مطلوب")
        self.state.mutate(lambda data: data.setdefault("rooms", {}).setdefault(key, {}).update({"custom_enabled": bool(enabled)}))

    def set_default(self, room: str, template: str) -> str:
        value = str(template or "").strip()
        if not value:
            raise ValueError("اكتب نص الترحيب بعد +wc@")
        if len(value.encode("utf-8")) > MAX_TEMPLATE_BYTES:
            raise ValueError(f"نص الترحيب طويل؛ الحد {MAX_TEMPLATE_BYTES} بايت.")
        key = normalize(room)
        if not key:
            raise ValueError("اسم الغرفة مطلوب")
        self.state.mutate(lambda data: data.setdefault("rooms", {}).setdefault(key, {}).update({"template": value}))
        return value

    def set_user(self, room: str, username: str, template: str) -> str:
        room_key, user_key = normalize(room), normalize(username)
        value = str(template or "").strip()
        if not room_key or not user_key or not value:
            raise ValueError("الصيغة: +wcs@اسم المستخدم@نص الترحيب")
        if len(value.encode("utf-8")) > MAX_TEMPLATE_BYTES:
            raise ValueError(f"نص الترحيب طويل؛ الحد {MAX_TEMPLATE_BYTES} بايت.")
        def update(data):
            room_data = data.setdefault("rooms", {}).setdefault(room_key, {})
            room_data.setdefault("users", {})[user_key] = value
        self.state.mutate(update)
        return value

    def render(self, room: str, username: str, user_id: str = "") -> str | None:
        settings = self._room(room)
        if not settings.get("enabled", False):
            return None
        user_key = normalize(username)
        custom = settings.get("users", {})
        template = ""
        if settings.get("custom_enabled", False) and isinstance(custom, dict):
            template = str(custom.get(user_key, "") or "")
        if not template:
            template = str(settings.get("template") or DEFAULT_TEMPLATE)
        return (template.replace("{room}", str(room or ""))
                .replace("{username}", str(username or ""))
                .replace("{id}", str(user_id or "")))
