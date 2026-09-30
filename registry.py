"""Persistent registry and authorization model for inserted Talkin bots."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from state import CredentialVault, JsonState, StateError, normalize, status_value


class RegistryError(ValueError):
    """Raised for an invalid server command or an unauthorized registry action."""


@dataclass(frozen=True)
class BotSpec:
    username: str
    password: str
    room: str
    role: str
    master: str


def _empty_state() -> dict[str, Any]:
    return {"version": 1, "bots": [], "languages": {"pending": {}, "selected": {}}}


class BotRegistry:
    def __init__(self, data_dir: str | Path, encryption_key: str, admin_name: str):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.vault = CredentialVault(encryption_key)
        self.admin_name = str(admin_name or "").strip()
        self.state = JsonState(root / "talkin_bot_server.json", _empty_state)

    @staticmethod
    def _record_key(record: dict[str, Any]) -> str:
        return normalize(record.get("username", ""))

    @staticmethod
    def _room_key(record: dict[str, Any]) -> str:
        return normalize(record.get("room", ""))

    @staticmethod
    def _active(record: dict[str, Any]) -> bool:
        return str(record.get("status", "")).lower() not in {"deleted", "stopped"}

    def all(self) -> list[dict[str, Any]]:
        data = self.state.load()
        records = data.get("bots", []) if isinstance(data, dict) else []
        return [dict(item) for item in records if isinstance(item, dict)]

    def get(self, bot_id: str, include_password: bool = False) -> dict[str, Any] | None:
        target = str(bot_id or "")
        for record in self.all():
            if str(record.get("id", "")) == target:
                if include_password:
                    record["password"] = self.vault.open(record.get("password_encrypted", ""))
                record.pop("password_encrypted", None)
                return record
        return None

    def create(self, spec: BotSpec) -> dict[str, Any]:
        username = str(spec.username or "").strip().lstrip("@")
        password = str(spec.password or "")
        room = str(spec.room or "").strip()
        master = str(spec.master or "").strip().lstrip("@")
        role = str(spec.role or "").strip().lower()
        if not username or not password or not room or not master:
            raise RegistryError("اسم البوت وكلمة المرور والغرفة والماستر مطلوبة")
        if role not in {"controller", "silent"}:
            raise RegistryError("نوع البوت غير صحيح")

        def action(data: dict[str, Any]) -> dict[str, Any]:
            records = data.setdefault("bots", [])
            username_key, room_key = normalize(username), normalize(room)
            active = [item for item in records if isinstance(item, dict) and self._active(item)]
            if any(normalize(item.get("username")) == username_key for item in active):
                raise RegistryError("هذا الحساب مضاف بالفعل في سيرفر البوتات")
            if role == "controller" and any(
                normalize(item.get("room")) == room_key and item.get("role") == "controller" for item in active
            ):
                raise RegistryError("يوجد بوت متحكم بالفعل لهذه الغرفة")
            existing_room = next((item for item in active if normalize(item.get("room")) == room_key), None)
            if existing_room:
                authorized = {normalize(existing_room.get("master"))}
                authorized.update(normalize(x) for x in existing_room.get("delegates", []) if x)
                if normalize(master) not in authorized:
                    raise RegistryError("هذه الغرفة مرتبطة بالماستر المسجّل والماسترات المضافين فقط")
            now = int(time.time())
            record = {
                "id": uuid.uuid4().hex,
                "username": username,
                "password_encrypted": self.vault.seal(password),
                "room": room,
                "role": role,
                "master": existing_room.get("master") if existing_room else master,
                "delegates": list(existing_room.get("delegates", [])) if existing_room else [],
                "status": "starting",
                "pid": None,
                "created_at": now,
                "updated_at": now,
                "error": "",
                "profile_status": status_value(role, room, existing_room.get("master") if existing_room else master, self.admin_name),
            }
            records.append(record)
            return self._without_secret(record)

        return self.state.mutate(action)

    @staticmethod
    def _without_secret(record: dict[str, Any]) -> dict[str, Any]:
        visible = dict(record)
        visible.pop("password_encrypted", None)
        return visible

    def update_runtime(self, bot_id: str, status: str, pid: int | None = None, error: str = "") -> None:
        def action(data: dict[str, Any]) -> None:
            for record in data.get("bots", []):
                if isinstance(record, dict) and str(record.get("id")) == str(bot_id):
                    record["status"] = str(status)
                    if pid is not None:
                        record["pid"] = int(pid)
                    record["error"] = str(error or "")[:500]
                    record["updated_at"] = int(time.time())
                    return
            raise RegistryError("البوت غير موجود")
        self.state.mutate(action)

    def delete(self, room: str, requester: str, all_bots: bool = False) -> list[dict[str, Any]]:
        room_key = normalize(room)
        requester_key = normalize(requester)
        if not room_key:
            raise RegistryError("اسم الغرفة مطلوب")

        def action(data: dict[str, Any]) -> list[dict[str, Any]]:
            records = data.get("bots", [])
            related = [r for r in records if isinstance(r, dict) and self._active(r) and normalize(r.get("room")) == room_key]
            if not related:
                raise RegistryError("لا توجد بوتات مسجلة لهذه الغرفة")
            primary = related[0]
            authorized = {normalize(primary.get("master"))}
            authorized.update(normalize(x) for x in primary.get("delegates", []) if x)
            if requester_key not in authorized:
                raise RegistryError("هذا الأمر متاح لماستر الغرفة فقط")
            targets = related if all_bots else [r for r in related if r.get("role") == "controller"]
            if not targets:
                raise RegistryError("لا يوجد بوت متحكم لحذفه")
            now = int(time.time())
            for record in targets:
                record["status"] = "deleted"
                record["updated_at"] = now
            return [self._without_secret(r) for r in targets]
        return self.state.mutate(action)

    def add_delegate(self, room: str, requester: str, delegate: str) -> dict[str, Any]:
        room_key, requester_key = normalize(room), normalize(requester)
        delegate = str(delegate or "").strip().lstrip("@")
        if not room_key or not delegate:
            raise RegistryError("الصيغة: master@اسم_المستخدم@اسم_الغرفة")
        def action(data: dict[str, Any]) -> dict[str, Any]:
            related = [r for r in data.get("bots", []) if isinstance(r, dict) and self._active(r) and normalize(r.get("room")) == room_key]
            if not related:
                raise RegistryError("الغرفة غير مسجلة")
            record = related[0]
            if requester_key != normalize(record.get("master")):
                raise RegistryError("إضافة الماستر للماستر الأساسي فقط")
            delegates = list(record.get("delegates", []))
            if normalize(delegate) not in {normalize(x) for x in delegates} and normalize(delegate) != normalize(record.get("master")):
                delegates.append(delegate)
            for item in related:
                item["delegates"] = delegates
                item["updated_at"] = int(time.time())
            return self._without_secret(record)
        return self.state.mutate(action)

    def remove_delegate(self, room: str, requester: str, delegate: str) -> dict[str, Any]:
        room_key, requester_key, delegate_key = normalize(room), normalize(requester), normalize(delegate)
        if not room_key or not delegate_key:
            raise RegistryError("الصيغة: delmaster@اسم_المستخدم@اسم_الغرفة")
        def action(data: dict[str, Any]) -> dict[str, Any]:
            related = [r for r in data.get("bots", []) if isinstance(r, dict) and self._active(r) and normalize(r.get("room")) == room_key]
            if not related:
                raise RegistryError("الغرفة غير مسجلة")
            record = related[0]
            if requester_key != normalize(record.get("master")):
                raise RegistryError("حذف الماستر للماستر الأساسي فقط")
            delegates = [x for x in record.get("delegates", []) if normalize(x) != delegate_key]
            if len(delegates) == len(record.get("delegates", [])):
                raise RegistryError("هذا المستخدم ليس ماسترًا مضافًا")
            for item in related:
                item["delegates"] = delegates
                item["updated_at"] = int(time.time())
            return self._without_secret(record)
        return self.state.mutate(action)

    def masters(self, room: str, requester: str) -> tuple[str, list[str]]:
        room_key, requester_key = normalize(room), normalize(requester)
        related = [r for r in self.all() if self._active(r) and normalize(r.get("room")) == room_key]
        if not related:
            raise RegistryError("الغرفة غير مسجلة")
        record = related[0]
        authorized = {normalize(record.get("master"))}
        authorized.update(normalize(x) for x in record.get("delegates", []) if x)
        if requester_key not in authorized:
            raise RegistryError("هذا الأمر متاح لماستر الغرفة فقط")
        return str(record.get("master")), list(record.get("delegates", []))

    def visible_bots(self, requester: str) -> list[dict[str, Any]]:
        requester_key = normalize(requester)
        visible = []
        for record in self.all():
            if not self._active(record):
                continue
            masters = {normalize(record.get("master"))}
            masters.update(normalize(x) for x in record.get("delegates", []) if x)
            if requester_key in masters:
                visible.append(self._without_secret(record))
        return visible

    def set_pending_language(self, username: str) -> None:
        key = normalize(username)
        if not key:
            return
        def action(data: dict[str, Any]) -> None:
            languages = data.setdefault("languages", {"pending": {}, "selected": {}})
            languages.setdefault("pending", {})[key] = {"username": str(username).strip().lstrip("@"), "created_at": int(time.time())}
        self.state.mutate(action)

    def choose_language(self, username: str, choice: str) -> str | None:
        key, choice = normalize(username), str(choice or "").strip().casefold()
        value = "ar" if choice in {"1", "١", "ar", "عربي", "العربية"} else "en" if choice in {"2", "٢", "en", "english", "انجليزي", "الانجليزية"} else ""
        if not value:
            return None
        def action(data: dict[str, Any]) -> str | None:
            languages = data.setdefault("languages", {"pending": {}, "selected": {}})
            if key not in languages.setdefault("pending", {}):
                return None
            languages["pending"].pop(key, None)
            languages.setdefault("selected", {})[key] = value
            return value
        return self.state.mutate(action)

    def language(self, username: str) -> str:
        data = self.state.load()
        return str(data.get("languages", {}).get("selected", {}).get(normalize(username), "ar"))
