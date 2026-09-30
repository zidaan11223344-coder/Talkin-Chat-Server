"""Restricted child runtime: Talkin administration, protection, invitations and lists only."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from state import JsonState, normalize

# Child environment is populated by runner.py before this module is imported.
from vendor.talkin_runtime import (  # noqa: E402
    BOT_ID,
    BOT_MASTER,
    TalkinBot,
    _norm_room,
    _norm_user,
    decode_result_message,
)


class RestrictedTalkinBot(TalkinBot):
    """A Talkin account that intentionally exposes only the requested room tools."""

    def __init__(self, record_id: str, registry_root: str):
        self.record_id = str(record_id)
        self.registry_root = Path(registry_root)
        self.role = os.getenv("BOT_ENTRY_TYPE", "controller").strip().casefold()
        self.master = os.getenv("BOT_MASTER", "").strip().lstrip("@")
        self.target_room = os.getenv("GROUP_TO_JOIN", "").strip()
        self._protection_state = JsonState(
            Path(os.getenv("BOT_DATA_DIR", self.registry_root / "children" / self.record_id)) / "protection.json",
            lambda: {"rooms": {}},
        )
        self._flood = defaultdict(lambda: {"sender": "", "body": "", "sender_count": 0, "body_count": 0, "at": 0.0})
        super().__init__()
        # The inherited runtime has invitation support, but no unrequested
        # games/media/publication commands are dispatched by this subclass.
        self.invites_enabled = True

    def _is_master(self, username: str) -> bool:
        return bool(normalize(username) and normalize(username) == normalize(self.master))

    def _is_own_room(self, room: str) -> bool:
        return normalize(room) == normalize(self.target_room)

    def _security(self, room: str) -> dict[str, Any]:
        data = self._protection_state.load()
        rooms = data.get("rooms", {}) if isinstance(data, dict) else {}
        current = rooms.get(_norm_room(room), {}) if isinstance(rooms, dict) else {}
        return {
            "words_enabled": bool(current.get("words_enabled", False)),
            "flood_enabled": bool(current.get("flood_enabled", False)),
            "repeat_limit": max(2, min(50, int(current.get("repeat_limit", 3) or 3))),
            "words": [str(x).strip() for x in current.get("words", []) if str(x).strip()],
        }

    def _update_security(self, room: str, **changes: Any) -> dict[str, Any]:
        room_key = _norm_room(room)
        def update(data: dict[str, Any]) -> dict[str, Any]:
            rooms = data.setdefault("rooms", {})
            item = rooms.setdefault(room_key, {"words_enabled": False, "flood_enabled": False, "repeat_limit": 3, "words": []})
            item.update(changes)
            item["repeat_limit"] = max(2, min(50, int(item.get("repeat_limit", 3) or 3)))
            item["words"] = list(dict.fromkeys(str(x).strip() for x in item.get("words", []) if str(x).strip()))
            return dict(item)
        return self._protection_state.mutate(update)

    def _send_help(self, room: str) -> None:
        self.send_room_text(
            room,
            "🛡️ أوامر بوت التحكم S-Boot\n"
            "a@اسم — مشرف | o@اسم — أونر\n"
            "b@اسم — حظر | u@اسم — فك حظر | k@اسم — طرد\n"
            "حماية — قائمة الحماية\n"
            "تشغيل الحماية / إيقاف الحماية\n"
            "+mf@كلمة / -mf@كلمة / l@mf / mr@3\n"
            "inv — دعوة مستخدمي الغرفة\n"
            "i@اسم — دعوة مستخدم\n"
            "l@m / l@a / l@o / l@b / l@all — عرض القوائم\n"
            "⚠️ الأوامر للماستر المسجّل فقط داخل الغرفة."
        )

    def _send_protection_menu(self, room: str) -> None:
        cfg = self._security(room)
        self.send_room_text(
            room,
            "🛡️ حماية الغرفة\n"
            f"• فلتر الكلمات: {'🟢' if cfg['words_enabled'] else '🔴'}\n"
            f"• منع التكرار: {'🟢' if cfg['flood_enabled'] else '🔴'}\n"
            f"• حد التكرار: {cfg['repeat_limit']}\n\n"
            "تشغيل الحماية: يشغّل الفلتر ومنع التكرار\n"
            "إيقاف الحماية: يوقفهما\n"
            "+mf@كلمة لإضافة كلمة | -mf@كلمة للحذف\n"
            "l@mf لعرض الكلمات | clear@mf للتنظيف\n"
            "mr@2 إلى mr@50 لتحديد حد التكرار"
        )

    def _moderate(self, room: str, target: str, operation: str) -> None:
        target = str(target or "").strip().lstrip("@")
        if not target:
            self.send_room_text(room, "❌ أرسل اسم المستخدم بعد @.")
            return
        if normalize(target) == normalize(BOT_ID):
            self.send_room_text(room, "❌ لا يمكن للبوت تنفيذ الإجراء على نفسه.")
            return
        try:
            self.request_admin_action(room, target, operation, self.master, announce_room=True)
        except Exception as exc:
            self.log("[S-BOOT] moderation failed", repr(exc))
            self.send_room_text(room, "❌ تعذر إرسال أمر الإدارة إلى Talkin.")

    def _start_invites(self, room: str, requester: str) -> None:
        """Invite the current room roster without an unreliable owners-list gate.

        Some Talkin builds return an incomplete ``owners_list`` response even
        when the account is already owner.  Controller access is established at
        insertion time, so ``inv`` reads the room roster directly and sends the
        normal private invites without rejecting a verified owner on that stale
        secondary list.
        """
        if self.invite_pending:
            self.send_room_text(room, "⏳ توجد عملية دعوات قيد التنفيذ بالفعل.")
            return
        self.invite_pending = True
        self.invite_silent_master = False
        self.invite_room = room
        self.invite_sent.clear()
        self._inv_response_room = room
        self._inv_response_to = ""
        try:
            roster = self._settings_room_users(room)
        except Exception as exc:
            roster = []
            self.log("[S-BOOT] database roster unavailable", repr(exc))
        names = []
        seen = set()
        for item in roster:
            username = str((item or {}).get("username") or "").strip().lstrip("@")
            key = _norm_user(username)
            if username and key and key != _norm_user(BOT_ID) and key not in seen:
                seen.add(key)
                names.append(username)
        if names:
            self.send_room_text(room, f"⏳ جاري إرسال الدعوات إلى {len(names)} مستخدم.")
            self._sboot_invites = None
            threading.Thread(target=self._finish_invites, args=(room, names), name="sboot-invites", daemon=True).start()
            return
        self._sboot_invites = {"room": room, "requester": requester, "started_at": time.time()}
        try:
            self.send_room_text(room, "⏳ جاري جلب قائمة الغرفة لإرسال الدعوات.")
            from vendor.talkin_runtime import encode_query
            self.send_query(encode_query("room_admin", type_="occupants_list", room=room, to=BOT_ID, value="none"))
        except Exception as exc:
            self.invite_pending = False
            self._sboot_invites = None
            self.log("[S-BOOT] native invitation roster failed", repr(exc))
            self.send_room_text(room, "❌ تعذر جلب قائمة الغرفة للدعوات.")

    def _handle_invitation_roster(self, result: dict[str, Any]) -> bool:
        pending = getattr(self, "_sboot_invites", None)
        if not isinstance(pending, dict):
            return False
        room = str(pending.get("room") or "").strip()
        users = self._extract_room_list_users(result)
        if not users:
            return False
        names, seen = [], set()
        for item in users:
            username = str(item.get("username") or "").strip().lstrip("@")
            key = _norm_user(username)
            if username and key and key != _norm_user(BOT_ID) and key not in seen:
                seen.add(key)
                names.append(username)
        self._sboot_invites = None
        if not names:
            self.invite_pending = False
            self.send_room_text(room, "📭 لا يوجد مستخدمون في القائمة لإرسال الدعوات.")
            return True
        self.send_room_text(room, f"⏳ تم جلب {len(names)} مستخدم. بدأ إرسال الدعوات.")
        threading.Thread(target=self._finish_invites, args=(room, names), name="sboot-native-invites", daemon=True).start()
        return True

    def _handle_protection_command(self, room: str, text: str) -> bool:
        low = text.casefold().strip()
        if low in {"حماية", "حمايه", "حماية الغرفة", "حمايه الغرفه"}:
            self._send_protection_menu(room)
            return True
        if low in {"تشغيل الحماية", "تشغيل الحمايه"}:
            self._update_security(room, words_enabled=True, flood_enabled=True)
            self.send_room_text(room, "🛡️ تم تشغيل حماية الكلمات ومنع التكرار.")
            return True
        if low in {"إيقاف الحماية", "ايقاف الحماية", "إيقاف الحمايه", "ايقاف الحمايه"}:
            self._update_security(room, words_enabled=False, flood_enabled=False)
            self.send_room_text(room, "⛔ تم إيقاف حماية الغرفة.")
            return True
        if low == "mf@on":
            self._update_security(room, words_enabled=True)
            self.send_room_text(room, "🛡️ تم تشغيل فلتر الكلمات.")
            return True
        if low == "mf@off":
            self._update_security(room, words_enabled=False)
            self.send_room_text(room, "⛔ تم إيقاف فلتر الكلمات.")
            return True
        if low.startswith("+mf@"):
            word = text.split("@", 1)[1].strip()
            if not word:
                self.send_room_text(room, "❌ الصيغة: +mf@كلمة")
                return True
            cfg = self._security(room)
            cfg["words"].append(word)
            self._update_security(room, words=cfg["words"])
            self.send_room_text(room, f"✅ تمت إضافة الكلمة الممنوعة: {word}")
            return True
        if low.startswith("-mf@"):
            word = text.split("@", 1)[1].strip()
            cfg = self._security(room)
            cfg["words"] = [item for item in cfg["words"] if normalize(item) != normalize(word)]
            self._update_security(room, words=cfg["words"])
            self.send_room_text(room, f"✅ تمت إزالة الكلمة: {word}")
            return True
        if low == "l@mf":
            words = self._security(room)["words"]
            text_value = "\n".join(f"{index}. {word}" for index, word in enumerate(words, 1)) or "لا توجد كلمات ممنوعة."
            self.send_room_text(room, "🚫 كلمات الفلتر:\n" + text_value)
            return True
        if low == "clear@mf":
            self._update_security(room, words=[])
            self.send_room_text(room, "🧹 تم حذف كلمات الفلتر.")
            return True
        match = re.fullmatch(r"mr@(\d+)", low)
        if match:
            count = int(match.group(1))
            if not 2 <= count <= 50:
                self.send_room_text(room, "❌ حد التكرار من 2 إلى 50.")
                return True
            self._update_security(room, repeat_limit=count)
            self.send_room_text(room, f"✅ تم ضبط حد التكرار على {count}.")
            return True
        return False

    def _handle_controller_command(self, room: str, sender: str, text: str) -> bool:
        """Dispatch only the requested management, protection, invite and list commands."""
        if self.role != "controller" or not self._is_own_room(room):
            return False
        low = str(text or "").strip().casefold()
        if not low:
            return False
        if not self._is_master(sender):
            # Prevent non-masters from probing any control command.
            return low.startswith(("a@", "o@", "b@", "u@", "k@", "حماية", "حمايه", "inv", "i@", "l@", "+mf@", "-mf@", "mf@", "mr@"))
        if low in {"help", "مساعدة", "الاوامر", "الأوامر"}:
            self._send_help(room)
            return True
        if self._handle_protection_command(room, text):
            return True
        short = re.fullmatch(r"(a|o|b|u|k)@(.+)", str(text).strip(), re.I)
        if short:
            mapping = {"a": "admin", "o": "owner", "b": "ban", "u": "member", "k": "kick"}
            self._moderate(room, short.group(2), mapping[short.group(1).casefold()])
            return True
        long_form = re.fullmatch(r"(admin|owner|ban|unban|kick)\s+@?([^\s@]+)", str(text).strip(), re.I)
        if long_form:
            mapping = {"admin": "admin", "owner": "owner", "ban": "ban", "unban": "member", "kick": "kick"}
            self._moderate(room, long_form.group(2), mapping[long_form.group(1).casefold()])
            return True
        if low in {"inv", "دعوات", "invite"}:
            try:
                self._start_invites(room, sender)
            except Exception as exc:
                self.log("[S-BOOT] invitation request failed", repr(exc))
                self.send_room_text(room, "❌ تعذر بدء الدعوات.")
            return True
        direct = re.fullmatch(r"i@(.+)", str(text).strip(), re.I)
        if direct:
            target = direct.group(1).strip().lstrip("@")
            try:
                sent = self.send_private_invite(target, room, inviter=sender)
                self.send_room_text(room, f"✅ تم إرسال الدعوة إلى @{target}." if sent else f"⚠️ تعذر إرسال الدعوة إلى @{target}.")
            except Exception:
                self.send_room_text(room, f"❌ تعذر إرسال الدعوة إلى @{target}.")
            return True
        if low in {"l@m", "l@a", "l@o", "l@b", "l@all", "l@*", "l@x"}:
            return bool(self._room_list_commands(room, text, sender, is_private=False))
        return False

    def _apply_protection(self, room: str, sender: str, body: str) -> bool:
        if self.role != "controller" or not self._is_own_room(room) or self._is_master(sender):
            return False
        cfg = self._security(room)
        clean = normalize(body)
        if cfg["words_enabled"]:
            hit = next((word for word in cfg["words"] if normalize(word) and normalize(word) in clean), None)
            if hit:
                try:
                    self.send_admin(room, sender, "ban")
                    self.send_room_text(room, f"🚫 تم حظر @{sender} بسبب كلمة ممنوعة.")
                except Exception as exc:
                    self.log("[S-BOOT] filter ban failed", repr(exc))
                return True
        if cfg["flood_enabled"]:
            state = self._flood[_norm_room(room)]
            now = time.time()
            within = now - float(state.get("at", 0.0)) <= 30.0
            same_sender = normalize(state.get("sender")) == normalize(sender)
            same_body = normalize(state.get("body")) == clean
            state["sender_count"] = int(state.get("sender_count", 0)) + 1 if within and same_sender else 1
            state["body_count"] = int(state.get("body_count", 0)) + 1 if within and same_body else 1
            state.update({"sender": sender, "body": body, "at": now})
            if max(state["sender_count"], state["body_count"]) >= cfg["repeat_limit"]:
                try:
                    self.send_admin(room, sender, "ban")
                    self.send_room_text(room, f"🚫 تم حظر @{sender} بسبب تكرار الرسائل.")
                except Exception as exc:
                    self.log("[S-BOOT] flood ban failed", repr(exc))
                state.clear()
                return True
        return False

    def _confirm_admin_change(self, room: str, event: dict[str, Any]) -> None:
        changed_user = str(event.get(17, "") or event.get(22, "") or "").strip()
        changed_role = str(event.get(31, "") or event.get(8, "") or "").strip().lower()
        if not changed_user or not changed_role:
            return
        self.room_users[room][changed_user] = changed_role
        key = (room.casefold(), changed_user.casefold(), changed_role)
        with self.pending_admin_lock:
            pending = self.pending_admin_actions.pop(key, None)
        if pending and pending.get("announce_room"):
            label = {
                "kicked": f"✅ تم طرد @{changed_user} من الغرفة.",
                "outcast": f"🚫 تم حظر @{changed_user} من الغرفة.",
                "member": f"✅ تم فك حظر @{changed_user}.",
                "admin": f"🛡️ تم رفع @{changed_user} إلى مشرف.",
                "owner": f"👑 تم رفع @{changed_user} إلى أونر.",
            }.get(changed_role)
            if label:
                self.send_room_text(room, label)

    def handle_room_event(self, result: dict[str, Any]) -> None:
        event = result.get("room_event") or {}
        event_type = str(event.get(1, "")).strip()
        sender = str(event.get(2, "")).strip()
        username = str(event.get(22, "") or event.get(17, "") or "").strip()
        body = str(event.get(6, "")).strip()
        room = str(event.get(13, self.target_room) or self.target_room).strip()
        if not room:
            return
        if event_type in {"you_joined", "you_rejoined"}:
            self.connected_rooms.add(room)
            self.last_joined_room = room
            self.request_room_occupants(room)
            try:
                from registry import BotRegistry
                registry = BotRegistry(self.registry_root, os.environ["STATE_ENCRYPTION_KEY"], os.getenv("SERVER_ADMIN_NAME", ""))
                registry.update_runtime(self.record_id, "online", os.getpid())
            except Exception as exc:
                self.log("[S-BOOT] runtime-state update failed", repr(exc))
        elif event_type == "user_joined" and username:
            self.room_users[room][username] = str(event.get(8, "") or "none").lower()
        elif event_type == "user_left" and username:
            self.room_users[room].pop(username, None)
        elif event_type == "role_changed":
            self._confirm_admin_change(room, event)
        if event_type == "text" and body and sender and normalize(sender) != normalize(BOT_ID):
            if not self._handle_controller_command(room, sender, body):
                self._apply_protection(room, sender, body)
        uid = str(result.get("uid", "") or "")
        if uid:
            try:
                self.ack(uid)
            except Exception:
                pass

    def _process_message(self, ws: Any, message: bytes) -> None:
        """Decode only room/private/event-list frames needed by the restricted bot."""
        try:
            result = decode_result_message(message)
            result_type = str(result.get("type") or "").strip()
            room_value = str(result.get("value") or "").strip()
            if result_type == "success" and room_value and _norm_room(room_value) in self._pending_room_joins:
                self.handle_room_event({"room_event": {1: "you_joined", 13: room_value}, "uid": result.get("uid", "")})
            if result.get("room_event"):
                self.handle_room_event(result)
            if result.get("users") or result.get("room_admin"):
                if not result.get("_occupants_room") and len(self._pending_room_lists) == 1:
                    result["_occupants_room"] = next(iter(self._pending_room_lists))
                if not self._handle_invitation_roster(result):
                    self.process_occupants_for_invite(result)
                    self._complete_pending_room_list(result)
            chat = result.get("chat_message") or {}
            sender = str(chat.get(3, "") or "").strip()
            body = str(chat.get(5, "") or "").strip()
            if sender and body and self._is_master(sender):
                self.send_private_text(sender, "ℹ️ أوامر بوت التحكم تُنفذ داخل الغرفة المرتبط بها البوت فقط.")
            if result.get("uid"):
                self.ack(str(result["uid"]))
        except Exception as exc:
            self.last_error = str(exc)
            self.log("[S-BOOT] message processing failed", repr(exc))

    def start(self) -> None:
        try:
            super().start()
        finally:
            try:
                from registry import BotRegistry
                registry = BotRegistry(self.registry_root, os.environ["STATE_ENCRYPTION_KEY"], os.getenv("SERVER_ADMIN_NAME", ""))
                registry.update_runtime(self.record_id, "offline", None)
            except Exception:
                pass


if __name__ == "__main__":
    print("Run this module through runner.py", file=sys.stderr)
    raise SystemExit(2)
