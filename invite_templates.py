"""Persistent per-room private invitation message templates."""
from __future__ import annotations

from pathlib import Path

from state import JsonState, normalize


DEFAULT_TEMPLATE = "يوجد معجب مخفي في ({room})"
MAX_TEMPLATE_BYTES = 700


def _blank():
    return {"rooms": {}}


class InviteTemplateStore:
    def __init__(self, root: str | Path):
        path = Path(root)
        if path.suffix.lower() != ".json":
            path = path / "invite_templates.json"
        self.state = JsonState(path, _blank)

    @staticmethod
    def ensure_room(template: str) -> str:
        value = str(template or "").strip()
        if "{room}" not in value:
            value = value.rstrip() + " ({room})"
        elif "({room})" not in value:
            value = value.replace("{room}", "({room})", 1)
        return value

    def get(self, room: str, fallback: str = DEFAULT_TEMPLATE) -> str:
        data = self.state.load()
        rooms = data.get("rooms", {}) if isinstance(data, dict) else {}
        value = rooms.get(normalize(room)) if isinstance(rooms, dict) else None
        return str(value or fallback)

    def set(self, room: str, template: str) -> str:
        key = normalize(room)
        value = self.ensure_room(template)
        if not key or not str(template or "").strip():
            raise ValueError("اكتب نص الدعوة بعد invmsg@")
        if len(value.encode("utf-8")) > MAX_TEMPLATE_BYTES:
            raise ValueError(f"رسالة الدعوة طويلة؛ الحد {MAX_TEMPLATE_BYTES} بايت.")
        def mutate(data):
            data.setdefault("rooms", {})[key] = value
        self.state.mutate(mutate)
        return value

    def reset(self, room: str) -> None:
        key = normalize(room)
        def mutate(data):
            data.setdefault("rooms", {}).pop(key, None)
        self.state.mutate(mutate)

    def render(self, room: str, username: str, sender: str, fallback: str = DEFAULT_TEMPLATE) -> str:
        template = self.ensure_room(self.get(room, fallback))
        return (template.replace("{room}", str(room or ""))
                .replace("{username}", str(username or ""))
                .replace("{sender}", str(sender or "")))
