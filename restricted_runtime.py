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
from cricket_game import CricketGame
from invite_templates import InviteTemplateStore
from room_actions import RoomActionQueue

# Child environment is populated by runner.py before this module is imported.
from vendor.talkin_runtime import (  # noqa: E402
    BOT_ID,
    BOT_MASTER,
    TalkinBot,
    _norm_room,
    _norm_user,
    decode_result_message,
    encode_query,
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
        self.invites_enabled = True
        self._invite_templates = InviteTemplateStore(self.registry_root)
        self._room_actions = RoomActionQueue(self.registry_root)
        self._room_action_lock = threading.Lock()
        self._cricket = CricketGame(self.registry_root)
        self._cricket_cursor = self._cricket.latest_event_id(self.target_room)
        self._cricket_delivery_lock = threading.Lock()
        self._shared_worker_stop = threading.Event()
        self._help_page_by_sender: dict[str, int] = {}

    def _is_master(self, username: str) -> bool:
        return bool(normalize(username) and normalize(username) == normalize(self.master))

    def _is_own_room(self, room: str) -> bool:
        return normalize(room) == normalize(self.target_room)

    @staticmethod
    def _is_owner_role(role: str) -> bool:
        return str(role or "").casefold().strip() in {
            "owner", "creator", "room_owner", "room_creator", "host",
            "مالك", "اونر", "أونر", "صانع", "صانع_الغرفة",
        }

    def _request_controller_rank(self, room: str) -> None:
        """Ask Talkin for the live roster before enabling a controller bot."""
        if self.role != "controller":
            return
        pending = getattr(self, "_sboot_rank_checks", None)
        if not isinstance(pending, set):
            pending = set()
            self._sboot_rank_checks = pending
        pending.add(_norm_room(room))
        try:
            self.send_query(encode_query(
                "room_admin", type_="occupants_list", room=room,
                to=BOT_ID, value="none",
            ))
        except Exception as exc:
            self.log("[S-BOOT] controller rank check failed", repr(exc))

    def _validate_controller_rank(self, room: str, result: dict[str, Any]) -> None:
        """Leave and notify the master when a controller is below owner rank."""
        pending = getattr(self, "_sboot_rank_checks", None)
        room_key = _norm_room(room)
        if self.role != "controller" or not isinstance(pending, set) or room_key not in pending:
            return
        users = self._extract_room_list_users(result)
        bot = next((u for u in users if _norm_user(u.get("username")) == _norm_user(BOT_ID)), None)
        if not isinstance(bot, dict):
            return
        role = str(bot.get("role") or "").casefold().strip()
        if not role:
            return
        pending.discard(room_key)
        self.bot_room_roles[room_key] = role
        if self._is_owner_role(role):
            return
        self.connected_rooms.discard(room)
        self.room_users.pop(room, None)
        self.send_private_text(
            BOT_MASTER,
            f"⚠️ البوت المتحكم @{BOT_ID} رتبته الحالية أقل من أونر في الغرفة {room}.\n"
            "ارفع البوت أونر ثم أعد المحاولة.",
        )
        self.leave_room(room)

    def _auto_rejoin_after_removal(self, room: str) -> None:
        """Rejoin only after a server removal, never after intentional leave."""
        room_key = _norm_room(room)
        if not room_key or room_key in getattr(self, "_sboot_rejoin_pending", set()):
            return
        if room_key in getattr(self, "_intentional_leaves", set()):
            return
        pending = getattr(self, "_sboot_rejoin_pending", None)
        if not isinstance(pending, set):
            pending = set()
            self._sboot_rejoin_pending = pending
        pending.add(room_key)
        self.connected_rooms.discard(room)
        self._schedule_auto_rejoin(room, delay=1.2)

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

    def _help_text(self, page: int = 1) -> str:
        if int(page) == 1:
            return (
                "📋 أوامر الإدارة — 1 | الحظر والطرد\n"
                "━━━━━━━━━━━━\n"
                "k@اسم — طرد عضو\n"
                "kick اسم — طرد عضو\n"
                "b@اسم — حظر عضو\n"
                "ban اسم — حظر عضو\n"
                "bl@اسم — حظر عضو بكل غرف السيرفر\n"
                "ub@اسم — فك الحظر\n"
                "u@اسم — فك الحظر\n"
                "unban اسم — فك الحظر\n"
                "m@اسم — إعطاء عضوية\n"
                ".u — تراجع عن آخر إجراء إداري للبوت\n\n"
                "📌 للقائمة التالية اكتب ns"
            )
        return (
            "📋 أوامر الإدارة — 2 | الحماية والدعوات والقوائم\n"
            "━━━━━━━━━━━━\n"
            "a@اسم — تعيين مشرف | o@اسم — تعيين أونر\n"
            "حماية — عرض إعدادات الحماية\n"
            "تشغيل الحماية / إيقاف الحماية\n"
            "+mf@كلمة / -mf@كلمة — إضافة/حذف كلمة ممنوعة\n"
            "l@mf — الكلمات الممنوعة | mr@2 إلى mr@50 — حد التكرار\n\n"
            "inv — دعوة الأونرات والمشرفين والأعضاء\n"
            "invmsg@النص ({room}) — تغيير نص الدعوة مع إبقاء اسم الغرفة\n"
            "invmsg@reset — استعادة نص الدعوة الافتراضي\n"
            "i@اسم — دعوة مستخدم | l@inv — الدعوات المرسلة برسالة واحدة\n\n"
            "l@m / l@a / l@o / l@b — قوائم الغرفة\n"
            "l@mas — عرض الماسترات\n"
            "mas@اسم — إضافة ماستر مساعد | umas@اسم — إزالته\n\n"
            "🏏 الكركيت: .cr 1 تشغيل | .cr 0 إيقاف\n"
            "كركيت 2 — مباراة تنتظر غرفتين | join — انضمام\n"
            "1 هجوم / 2 دفاع؛ بعد البداية يلعب الفريقان بالأرقام 0–6\n\n"
            "⚠️ إدارة الأوامر للماستر المسجّل فقط."
        )

    def _send_help(self, room: str, private_to: str = "", page: int = 1) -> None:
        text = self._help_text(page)
        if private_to:
            self.send_private_text(private_to, text)
        else:
            self.send_room_text(room, text)

    def _advance_restricted_list_page(self, room: str, sender: str) -> bool:
        pages = getattr(self, "_result_pages", {})
        if not isinstance(pages, dict):
            return False
        candidates = [
            (("room_message", str(room or ""), str(sender or "")), "room"),
            (("chat_message", str(room or ""), str(sender or "")), "private"),
        ]
        if not pages.get(candidates[1][0]):
            for candidate_key, candidate_state in reversed(list(pages.items())):
                if len(candidate_key) >= 3 and candidate_key[0] == "chat_message" and candidate_key[2] == str(sender or ""):
                    candidates.append((candidate_key, "private"))
                    break
        for key, destination in candidates:
            state = pages.get(key)
            if not isinstance(state, dict):
                continue
            entries = state.get("pages") or []
            index = int(state.get("part", 1) or 1)
            if index >= len(entries):
                continue
            index += 1
            state["part"] = index
            body = str(entries[index - 1])
            if index < len(entries):
                body += "\n\n📌 للقائمة التالية اكتب ns"
            else:
                body += "\n\n✅ انتهت القوائم."
            if destination == "private":
                self.send_private_text(sender, body)
            elif getattr(self, "_command_is_private", False):
                self.send_private_text(sender, body)
            else:
                self.send_room_text(room, body)
            return True
        response = "📌 لا توجد قائمة إضافية. أرسل help لعرض قائمة الأوامر."
        if getattr(self, "_command_is_private", False):
            self.send_private_text(sender, response)
        else:
            self.send_room_text(room, response)
        return False

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
            grant_membership = operation == "grant_member"
            native_operation = "member" if grant_membership else operation
            sent = self.request_admin_action(room, target, native_operation, self.master, announce_room=True)
            if grant_membership and sent:
                key = (room.casefold(), target.casefold(), "member")
                with self.pending_admin_lock:
                    pending = self.pending_admin_actions.get(key)
                    if pending is not None:
                        pending["membership_grant"] = True
                self.send_room_text(room, f"⏳ تم إرسال طلب منح العضوية إلى @{target}.")
            elif not sent:
                self.send_room_text(room, "❌ تعذر إرسال أمر الإدارة إلى Talkin.")
        except Exception as exc:
            self.log("[S-BOOT] moderation failed", repr(exc))
            self.send_room_text(room, "❌ تعذر إرسال أمر الإدارة إلى Talkin.")

    def _invite_message_for_room(self, room: str) -> str:
        fallback = getattr(self, "invite_message_template", "يوجد معجب مخفي في ({room})")
        return self._invite_templates.ensure_room(self._invite_templates.get(room, fallback))

    def _managed_controller_rooms(self) -> list[str]:
        from registry import BotRegistry
        registry = BotRegistry(self.registry_root, os.environ["STATE_ENCRYPTION_KEY"], os.getenv("SERVER_ADMIN_NAME", ""))
        rooms = {
            str(item.get("room") or "").strip()
            for item in registry.all()
            if isinstance(item, dict) and registry._active(item)
            and str(item.get("role") or "").casefold() == "controller"
            and str(item.get("room") or "").strip()
        }
        if self.target_room:
            rooms.add(self.target_room)
        return sorted(rooms, key=normalize)

    def _process_room_actions(self) -> None:
        if self.role != "controller" or not self.target_room or not self._room_action_lock.acquire(blocking=False):
            return
        try:
            for action in self._room_actions.pending_for_room(self.target_room):
                try:
                    sent = self.request_admin_action(
                        self.target_room,
                        str(action.get("target") or ""),
                        str(action.get("operation") or "ban"),
                        self.master,
                        announce_room=True,
                    )
                    if sent:
                        self._room_actions.mark_done(int(action["id"]), self.target_room)
                    else:
                        self._room_actions.defer(int(action["id"]), self.target_room)
                except Exception as exc:
                    self.log("[S-BOOT] shared room action failed", repr(exc))
                    self._room_actions.defer(int(action["id"]), self.target_room)
        finally:
            self._room_action_lock.release()

    def _send_invite_history(self, room: str, private_to: str = "") -> None:
        names = self.sent_invites(room)
        is_private = bool(private_to)
        query_type = "chat_message" if is_private else "room_message"

        def send(text: str) -> None:
            if is_private:
                self.send_private_text(private_to, text)
            else:
                self.send_room_text(room, text)

        def encoded_size(text: str) -> int:
            kwargs = {"type_": "text", "body": text}
            kwargs["to" if is_private else "room"] = private_to if is_private else room
            return len(encode_query(query_type, **kwargs))

        if not names:
            send("📨 الدعوات المرسلة\n━━━━━━━━━━━━\n📭 لا توجد دعوات مسجلة لهذه الغرفة.")
            return
        limit = max(240, int(os.getenv("WS_MAX_MESSAGE_BYTES", "1008")))
        header = f"📨 الدعوات المرسلة ({len(names)})\n━━━━━━━━━━━━\n"
        shown: list[str] = []
        for name in names:
            candidate = header + "، ".join("@" + item for item in shown + [name])
            if encoded_size(candidate) > limit - 48:
                break
            shown.append(name)
        omitted = len(names) - len(shown)
        text = header + "، ".join("@" + name for name in shown)
        if omitted:
            text += f"\n… وبقية الأسماء: {omitted} (الإجمالي {len(names)}) لضيق حد رسالة Talkin."
            while shown and encoded_size(text) > limit:
                shown.pop()
                omitted = len(names) - len(shown)
                text = header + "، ".join("@" + name for name in shown)
                text += f"\n… وبقية الأسماء: {omitted} (الإجمالي {len(names)}) لضيق حد رسالة Talkin."
        send(text)

    def _cricket_asset_url(self, filename: str) -> str:
        base = (os.getenv("CRICKET_ASSET_BASE_URL", "").strip()
                or "https://raw.githubusercontent.com/zidaan11223344-coder/Talkin-Chat-Server/main/vendor/assets").rstrip("/")
        return f"{base}/{filename}"

    def _deliver_cricket_events(self) -> None:
        if self.role != "controller" or not self.target_room or not self._cricket_delivery_lock.acquire(blocking=False):
            return
        try:
            events = self._cricket.events_after(self.target_room, self._cricket_cursor)
            for event in events:
                try:
                    for filename in event.get("images", []):
                        self.send_room_media(self.target_room, self._cricket_asset_url(filename), "image")
                    if event.get("text"):
                        self.send_room_text(self.target_room, str(event["text"]))
                    self._cricket_cursor = int(event.get("id", self._cricket_cursor))
                except Exception as exc:
                    self.log("[CRICKET] event delivery failed", repr(exc))
                    break
        finally:
            self._cricket_delivery_lock.release()

    def _cricket_game(self, room: str, sender: str, text: str) -> bool:
        """Route game/lobby commands to the shared cross-controller match."""
        low = str(text or "").strip().casefold().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        toggle = re.fullmatch(r"\.?cr\s*([01])", low)
        if toggle:
            if not self._is_master(sender):
                self.send_room_text(room, "🔒 تشغيل وإيقاف الكركيت للماستر فقط.")
                return True
            self.send_room_text(room, self._cricket.set_enabled(room, toggle.group(1) == "1"))
            self._deliver_cricket_events()
            return True
        start = re.fullmatch(r"(?:\.?cricket|كركيت|كريكت)\s+([2-8])", low)
        if start:
            error = self._cricket.start(room, int(start.group(1)))
            if error:
                self.send_room_text(room, error)
            self._deliver_cricket_events()
            return True
        if low in {"join", "انضمام"}:
            error = self._cricket.join(room)
            if error:
                self.send_room_text(room, error)
            self._deliver_cricket_events()
            return True
        match = self._cricket.current()
        if low in {"كركيت", "كريكت", "cricket", ".cricket"}:
            if match:
                self.send_room_text(room, f"🏏 مباراة الكركيت الحالية: {match.get('stage')} — الغرف {len(match.get('rooms', []))}/{match.get('target_rooms', 0)}.")
            else:
                self.send_room_text(room, "🏏 شغّل اللعبة بـ .cr 1 ثم ابدأ: كركيت 2. الغرف الأخرى تنضم بكتابة join.")
            return True
        if isinstance(match, dict) and match.get("stage") == "teams" and low in {"1", "2", "هجوم", "دفاع", "attack", "defense"}:
            team = "attack" if low in {"1", "هجوم", "attack"} else "defense"
            error = self._cricket.choose_team(room, team)
            if error:
                self.send_room_text(room, error)
            self._deliver_cricket_events()
            return True
        if isinstance(match, dict) and match.get("stage") == "live" and re.fullmatch(r"[0-6]", low):
            error = self._cricket.submit_ball(room, sender, int(low))
            if error:
                self.send_room_text(room, error)
            self._deliver_cricket_events()
            return True
        return False

    def _start_invites(self, room: str, requester: str) -> None:
        """Collect the same live room categories used by the list commands."""
        try:
            self.request_occupants(room=room, response_room=room, response_to=requester)
        except Exception as exc:
            self.log("[S-BOOT] invitation roster failed", repr(exc))
            self.send_room_text(room, "❌ تعذر جمع قوائم الغرفة للدعوات.")

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
        match_state = self._cricket.current()
        game_command = bool(
            re.fullmatch(r"\.?cr\s*[01٠١]", low)
            or re.fullmatch(r"(?:\.?cricket|كركيت|كريكت)(?:\s+[0-9٠-٩]+)?", low)
            or low in {"join", "انضمام"}
            or (isinstance(match_state, dict) and (
                (match_state.get("stage") == "teams" and low in {"1", "2", "هجوم", "دفاع", "attack", "defense"})
                or (match_state.get("stage") == "live" and re.fullmatch(r"[0-6٠-٦]", low))
            ))
        )
        if game_command:
            return self._cricket_game(room, sender, text)
        if low in {"help", "مساعدة", "الاوامر", "الأوامر"}:
            self._help_page_by_sender[_norm_user(sender)] = 1
            self._send_help(room, private_to=sender if getattr(self, "_command_is_private", False) else "", page=1)
            return True
        if low == "ns":
            user_key = _norm_user(sender)
            current_page = int(self._help_page_by_sender.get(user_key, 0) or 0)
            if current_page == 1:
                self._help_page_by_sender[user_key] = 2
                self._send_help(room, private_to=sender if getattr(self, "_command_is_private", False) else "", page=2)
            elif current_page == 2:
                response = "📌 وصلت إلى آخر قائمة الأوامر. أرسل help لعرض الصفحة الأولى."
                if getattr(self, "_command_is_private", False):
                    self.send_private_text(sender, response)
                else:
                    self.send_room_text(room, response)
            else:
                self._advance_restricted_list_page(room, sender)
            return True
        if not self._is_master(sender):
            # Prevent non-masters from probing any control command.
            return low.startswith(("a@", "o@", "b@", "bl@", "u@", "ub@", "k@", "m@", ".u", "حماية", "حمايه", "inv", "i@", "l@", "mas@", "umas@", "master@", "delmaster@", "+mf@", "-mf@", "mf@", "mr@"))
        if self._handle_protection_command(room, text):
            return True
        if low in {".u", "undo"}:
            self._undo_last_bot_action(sender)
            return True
        global_ban = re.fullmatch(r"bl@(.+)", str(text).strip(), re.I)
        if global_ban:
            target = global_ban.group(1).strip().lstrip("@")
            try:
                action_id, room_count = self._room_actions.enqueue(
                    target, self._managed_controller_rooms(), sender, operation="ban"
                )
                reply = f"✅ حُفظ طلب حظر @{target} في {room_count} غرفة. رقم العملية: {action_id}."
            except Exception as exc:
                reply = f"❌ تعذر إنشاء الحظر الشامل: {exc}"
            if getattr(self, "_command_is_private", False):
                self.send_private_text(sender, reply)
            else:
                self.send_room_text(room, reply)
            self._process_room_actions()
            return True
        short = re.fullmatch(r"(a|o|b|ub|u|k|m)@(.+)", str(text).strip(), re.I)
        if short:
            mapping = {"a": "admin", "o": "owner", "b": "ban", "ub": "member", "u": "member", "k": "kick", "m": "grant_member"}
            self._moderate(room, short.group(2), mapping[short.group(1).casefold()])
            return True
        long_form = re.fullmatch(r"(admin|owner|ban|unban|kick|member)\s+@?([^\s@]+)", str(text).strip(), re.I)
        if long_form:
            mapping = {"admin": "admin", "owner": "owner", "ban": "ban", "unban": "member", "kick": "kick", "member": "member"}
            self._moderate(room, long_form.group(2), mapping[long_form.group(1).casefold()])
            return True
        if low in {"inv", "دعوات", "invite"}:
            try:
                self._start_invites(room, sender)
            except Exception as exc:
                self.log("[S-BOOT] invitation request failed", repr(exc))
                self.send_room_text(room, "❌ تعذر بدء الدعوات.")
            return True
        invite_message = re.fullmatch(r"invmsg@(.+)", str(text).strip(), re.I | re.S)
        if invite_message:
            template = invite_message.group(1).strip()
            if template.casefold() == "reset":
                self._invite_templates.reset(room)
                reply = "✅ تمت استعادة نص الدعوة الافتراضي. اسم الغرفة يبقى ظاهرًا بين قوسين."
            else:
                try:
                    saved = self._invite_templates.set(room, template)
                    preview = saved.replace("{room}", room).replace("{sender}", sender).replace("{username}", "اسم_المستخدم")
                    reply = f"✅ تم حفظ نص الدعوة لهذه الغرفة.\nمعاينة: {preview}"
                except Exception as exc:
                    reply = f"❌ {exc}"
            if getattr(self, "_command_is_private", False):
                self.send_private_text(sender, reply)
            else:
                self.send_room_text(room, reply)
            return True
        if low in {"l@invmsg", "l@invite-message"}:
            template = self._invite_message_for_room(room)
            preview = template.replace("{room}", room).replace("{sender}", sender).replace("{username}", "اسم_المستخدم")
            reply = f"✉️ نص الدعوة الحالي لهذه الغرفة:\n{template}\n\nمعاينة: {preview}"
            if getattr(self, "_command_is_private", False):
                self.send_private_text(sender, reply)
            else:
                self.send_room_text(room, reply)
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
        master_cmd = re.fullmatch(r"(mas|master|umas|delmaster)@([^@\s]+)(?:@(.+))?", str(text).strip(), re.I)
        if master_cmd:
            command, target, explicit_room = master_cmd.groups()
            target_room = str(explicit_room or room or self.target_room).strip()
            if not target_room:
                self.send_private_text(sender, "❌ حدد الغرفة: mas@اسم_المستخدم@اسم_الغرفة")
                return True
            try:
                from registry import BotRegistry
                registry = BotRegistry(self.registry_root, os.environ["STATE_ENCRYPTION_KEY"], os.getenv("SERVER_ADMIN_NAME", ""))
                if command.casefold() in {"mas", "master"}:
                    registry.add_delegate(target_room, sender, target)
                    reply = f"✅ تمت إضافة @{target.lstrip('@')} كماستر للغرفة {target_room}."
                else:
                    registry.remove_delegate(target_room, sender, target)
                    reply = f"✅ تمت إزالة @{target.lstrip('@')} من ماسترات الغرفة {target_room}."
                if getattr(self, "_command_is_private", False):
                    self.send_private_text(sender, reply)
                else:
                    self.send_room_text(room, reply)
            except Exception as exc:
                reply = f"❌ {exc}"
                if getattr(self, "_command_is_private", False):
                    self.send_private_text(sender, reply)
                else:
                    self.send_room_text(room, reply)
            return True
        if low == "l@mas":
            try:
                from registry import BotRegistry
                registry = BotRegistry(self.registry_root, os.environ["STATE_ENCRYPTION_KEY"], os.getenv("SERVER_ADMIN_NAME", ""))
                primary, delegates = registry.masters(room, sender)
                names = [primary] + list(delegates)
                lines = ["👑 ماسترات الغرفة", "━━━━━━━━━━━━"]
                lines.extend(f"{index}. @{name}" for index, name in enumerate(names, 1))
                response = "\n".join(lines)
                if getattr(self, "_command_is_private", False):
                    self.send_private_text(sender, response)
                else:
                    self.send_room_text(room, response)
            except Exception as exc:
                response = f"❌ تعذر عرض الماسترات: {exc}"
                if getattr(self, "_command_is_private", False):
                    self.send_private_text(sender, response)
                else:
                    self.send_room_text(room, response)
            return True
        if low in {"l@inv", "l@invitations"}:
            self._send_invite_history(
                room,
                private_to=sender if getattr(self, "_command_is_private", False) else "",
            )
            return True
        if low in {"l@m", "l@a", "l@o", "l@b", "l@all", "l@*", "l@x"}:
            self._help_page_by_sender.pop(_norm_user(sender), None)
            return bool(self._room_list_commands(room, text, sender, is_private=getattr(self, "_command_is_private", False)))
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
            if changed_role == "member" and pending.get("membership_grant"):
                label = f"✅ تم منح @{changed_user} عضوية الغرفة."
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
            getattr(self, "_sboot_rejoin_pending", set()).discard(_norm_room(room))
            self._request_controller_rank(room)
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
            if normalize(username) == normalize(BOT_ID):
                self._auto_rejoin_after_removal(room)
        elif event_type == "role_changed":
            changed_user = str(event.get(17, "") or event.get(22, "") or "").strip()
            changed_role = str(event.get(31, "") or event.get(8, "") or "").casefold().strip()
            if normalize(changed_user) == normalize(BOT_ID) and changed_role in {"kicked", "outcast"}:
                self._auto_rejoin_after_removal(room)
            self._confirm_admin_change(room, event)
        if event_type == "text" and body and sender and normalize(sender) != normalize(BOT_ID):
            if not self._handle_controller_command(room, sender, body):
                self._apply_protection(room, sender, body)
        self._deliver_cricket_events()
        self._process_room_actions()
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
                rank_room = str(result.get("_occupants_room") or self.target_room or "").strip()
                if rank_room:
                    self._validate_controller_rank(rank_room, result)
                if not self._handle_invitation_roster(result):
                    # The list coordinator must consume each category first;
                    # otherwise the inherited inviter sends the first owner
                    # response immediately and skips admins/members.
                    if not self._complete_pending_room_list(result):
                        self.process_occupants_for_invite(result)
            chat = result.get("chat_message") or {}
            sender = str(chat.get(3, "") or "").strip()
            body = str(chat.get(5, "") or "").strip()
            if sender and body and (
                self._is_master(sender)
                or body.casefold().strip() in {"help", "مساعدة", "الاوامر", "الأوامر", "ns"}
            ):
                self._command_is_private = True
                try:
                    self._handle_controller_command(self.target_room, sender, body)
                finally:
                    self._command_is_private = False
            if result.get("uid"):
                self.ack(str(result["uid"]))
        except Exception as exc:
            self.last_error = str(exc)
            self.log("[S-BOOT] message processing failed", repr(exc))

    def start(self) -> None:
        self._shared_worker_stop.clear()
        if self.role == "controller":
            threading.Thread(target=self._shared_state_worker, name=f"shared-state-{self.record_id[:8]}", daemon=True).start()
        try:
            super().start()
        finally:
            self._shared_worker_stop.set()
            try:
                from registry import BotRegistry
                registry = BotRegistry(self.registry_root, os.environ["STATE_ENCRYPTION_KEY"], os.getenv("SERVER_ADMIN_NAME", ""))
                registry.update_runtime(self.record_id, "offline", None)
            except Exception:
                pass

    def _shared_state_worker(self) -> None:
        while not self._shared_worker_stop.wait(1.0):
            room_key = _norm_room(self.target_room)
            if not room_key or room_key not in {_norm_room(x) for x in self.connected_rooms}:
                continue
            self._deliver_cricket_events()
            self._process_room_actions()


if __name__ == "__main__":
    print("Run this module through runner.py", file=sys.stderr)
    raise SystemExit(2)
