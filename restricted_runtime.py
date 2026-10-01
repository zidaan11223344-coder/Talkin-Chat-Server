"""Restricted child runtime: Talkin administration, protection, invitations and lists only."""
from __future__ import annotations

import json
import os
import re
import secrets
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
    ASSETS_DIR,
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
        # The inherited runtime has invitation support, but no unrequested
        # games/media/publication commands are dispatched by this subclass.
        self.invites_enabled = True
        self._cricket_games = {}

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

    def _help_text(self) -> str:
        return (
            "🛡️ أوامر بوت التحكم S-Boot\n\n"
            "【 الإدارة 】\n"
            "a@اسم — إضافة مشرف\n"
            "o@اسم — إضافة أونر\n"
            "b@اسم — حظر\n"
            "u@اسم — فك الحظر\n"
            "k@اسم — طرد\n\n"
            "m@اسم — إعطاء عضوية\n\n"
            "【 الحماية 】\n"
            "حماية — عرض إعدادات الحماية\n"
            "تشغيل الحماية / إيقاف الحماية\n"
            "+mf@كلمة — إضافة كلمة ممنوعة\n"
            "-mf@كلمة — حذف كلمة ممنوعة\n"
            "l@mf — عرض الكلمات | clear@mf — تنظيفها\n"
            "mr@2 إلى mr@50 — تحديد حد التكرار\n\n"
            "【 الدعوات 】\n"
            "inv — جمع الأونرات والمشرفين والأعضاء وإرسال الدعوات\n"
            "i@اسم — إرسال دعوة خاصة إلى مستخدم واحد\n\n"
            "【 القوائم 】\n"
            "l@m — الأعضاء | l@a — المشرفون\n"
            "l@o — الأونرات | l@b — المحظورون\n"
            "l@all — عرض القوائم كلها\n"
            "l@mas — عرض الماسترات\n"
            "l@inv — عرض الدعوات المرسلة في رسالة واحدة\n\n"
            "【 الألعاب 】\n"
            "كركيت — لعبة لأعضاء الغرفة: اختر 1–4 لاعبين، هجوم/دفاع، 6 كرات، وهاتريك\n\n"
            "【 الماسترات 】\n"
            "mas@اسم_المستخدم — إضافة ماستر مساعد\n"
            "umas@اسم_المستخدم — إزالة ماستر مساعد\n\n"
            "⚠️ الأوامر للماستر المسجّل فقط داخل الغرفة."
        )

    def _send_help(self, room: str, private_to: str = "") -> None:
        text = self._help_text()
        if private_to:
            self.send_private_text(private_to, text)
        else:
            self.send_room_text(room, text)

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

    def _cricket_game(self, room: str, sender: str, text: str) -> bool:
        """Interactive room cricket: choose players, then attack/defense."""
        key = _norm_room(room)
        low = str(text or "").strip().casefold()
        game = self._cricket_games.get(key)
        if low in {"كركيت", "كريكت", "cricket"} and not game:
            self._cricket_games[key] = {"players": [sender], "max": 0, "choices": {}}
            self.send_room_text(room, "🏏 بدأت لعبة كركيت في الغرفة.\n👥 اكتب 1 أو 2 أو 3 أو 4 لاختيار عدد اللاعبين، ثم اكتب انضمام.")
            return True
        if not game:
            return False
        if low in {"كركيت", "كريكت", "cricket"}:
            self.send_room_text(room, "🏏 توجد لعبة كركيت قائمة. اكتب انضمام أو اختر العدد أولاً.")
            return True
        if low in {"1", "2", "3", "4"} and not game["max"] and _norm_user(sender) == _norm_user(game["players"][0]):
            game["max"] = int(low)
            if game["max"] == 1:
                game["players"].append("🤖 خصم")
                game["choices"] = {sender: "هجوم", "🤖 خصم": "دفاع"}
                return self._finish_cricket(room, game)
            self.send_room_text(room, f"✅ عدد اللاعبين: {low}. اكتب انضمام للمشاركة.")
            return True
        if low in {"انضمام", "join"} and game["max"]:
            if len(game["players"]) < game["max"] and _norm_user(sender) not in {_norm_user(x) for x in game["players"]}:
                game["players"].append(sender)
                remaining = game["max"] - len(game["players"])
                self.send_room_text(room, "✅ انضممت للعبة." + (f" باقي {remaining} لاعب." if remaining else "\n🎯 اكتمل العدد؛ اختر هجوم أو دفاع."))
            return True
        if low in {"هجوم", "دفاع", "attack", "defense"} and game["max"] and len(game["players"]) >= game["max"]:
            choice = "هجوم" if low in {"هجوم", "attack"} else "دفاع"
            game["choices"][sender] = choice
            if len(game["choices"]) >= len(game["players"]):
                if len({game["choices"].get(p) for p in game["players"]}) < 2:
                    self.send_room_text(room, "⚠️ يجب أن يوجد لاعب في الهجوم ولاعب في الدفاع.")
                    return True
                return self._finish_cricket(room, game)
            self.send_room_text(room, f"✅ تم اختيار {choice}. بانتظار بقية اللاعبين.")
            return True
        return True

    def _finish_cricket(self, room: str, game: dict) -> bool:
        attack = [p for p in game["players"] if game["choices"].get(p) == "هجوم"]
        defense = [p for p in game["players"] if game["choices"].get(p) == "دفاع"]
        score = wickets = streak = 0
        hat_trick = False
        duck_out = False
        balls = []
        for number in range(1, 7):
            attacker, defender = attack[(number - 1) % len(attack)], defense[(number - 1) % len(defense)]
            if secrets.randbelow(6) == 0:
                wickets += 1; streak += 1; outcome = "ويكيت"; hat_trick = hat_trick or streak >= 3
                if number == 1:
                    duck_out = True
                    outcome = "دَك — خروج من أول كرة"
            else:
                streak = 0; runs = secrets.randbelow(7); score += runs; outcome = f"{runs} رنز"
            balls.append(f"{number}. @{attacker} ضد @{defender}: {outcome}")
        defense_score = wickets * 2
        winner = "الهجوم" if score > defense_score else "الدفاع" if defense_score > score else "تعادل"
        self.send_room_text(room, "🏏 نتيجة لعبة كركيت\n━━━━━━━━━━━━\n" + "\n".join(balls) +
                             f"\n\n⚔️ نقاط الهجوم: {score}\n🛡️ نقاط الدفاع: {defense_score}\n🏆 الفائز: فريق {winner}\n🔥 هاتريك: {'نعم — 3 ويكيت متتالية' if hat_trick else 'لا'}" +
                             ("\n🦆 دَك! خرج اللاعب من أول كرة." if duck_out else ""))
        if duck_out:
            try:
                duck_url = self._game_public_image(ASSETS_DIR / "cricket_duck.png", route="assets")
                if duck_url:
                    self.send_room_media(room, duck_url, "image")
            except Exception as exc:
                self.log("[CRICKET] duck image failed", repr(exc))
        self._cricket_games.pop(_norm_room(room), None)
        return True

    def _start_invites(self, room: str, requester: str) -> None:
        """Collect the same live room categories used by the list commands."""
        try:
            self.request_occupants(room=room, response_room=room)
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
        if low in {"كركيت", "كريكت", "cricket", "join", "انضمام", "هجوم", "دفاع", "attack", "defense", "1", "2", "3", "4"}:
            return self._cricket_game(room, sender, text)
        if not self._is_master(sender):
            # Prevent non-masters from probing any control command.
            return low.startswith(("a@", "o@", "b@", "u@", "k@", "حماية", "حمايه", "inv", "i@", "l@", "mas@", "umas@", "master@", "delmaster@", "+mf@", "-mf@", "mf@", "mr@"))
        if low in {"help", "مساعدة", "الاوامر", "الأوامر"}:
            # Help is always private, even when the command was sent in a room.
            self._send_help(room, private_to=sender)
            return True
        if self._handle_protection_command(room, text):
            return True
        short = re.fullmatch(r"(a|o|b|u|k|m)@(.+)", str(text).strip(), re.I)
        if short:
            mapping = {"a": "admin", "o": "owner", "b": "ban", "u": "member", "k": "kick", "m": "member"}
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
                self.send_room_text(room, "\n".join(lines))
            except Exception as exc:
                self.send_room_text(room, f"❌ تعذر عرض الماسترات: {exc}")
            return True
        if low in {"l@inv", "l@invitations"}:
            names = self.sent_invites(room)
            lines = ["📨 الدعوات المرسلة", "━━━━━━━━━━━━"]
            if names:
                lines.extend(f"{index}. @{name}" for index, name in enumerate(names, 1))
                lines.append(f"\n📊 الإجمالي: {len(names)}")
            else:
                lines.append("📭 لم تُرسل دعوات مسجلة لهذه الغرفة.")
            self.send_room_text(room, "\n".join(lines))
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
                    self.process_occupants_for_invite(result)
                    self._complete_pending_room_list(result)
            chat = result.get("chat_message") or {}
            sender = str(chat.get(3, "") or "").strip()
            body = str(chat.get(5, "") or "").strip()
            if sender and body and self._is_master(sender):
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
