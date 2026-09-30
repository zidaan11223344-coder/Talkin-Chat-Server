"""S-Boot server command handling and supervised child-process management."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from registry import BotRegistry, BotSpec, RegistryError
from state import normalize


class BotServerService:
    def __init__(self, data_dir: str | Path, encryption_key: str, admin_name: str, spawn: bool = True):
        self.data_dir = Path(data_dir).resolve()
        self.registry = BotRegistry(self.data_dir, encryption_key, admin_name)
        self.spawn_enabled = bool(spawn)
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self.root = Path(__file__).resolve().parent

    def start_record(self, record: dict) -> None:
        bot_id = str(record.get("id") or "")
        if not bot_id or not self.spawn_enabled or self._stopping.is_set():
            return
        with self._lock:
            old = self._processes.get(bot_id)
            if old and old.poll() is None:
                return
            env = os.environ.copy()
            env["BOT_SERVER_DATA_DIR"] = str(self.data_dir)
            env["ASSET_HTTP_ENABLED"] = "0"
            env["GITHUB_SYNC"] = "0"
            process = subprocess.Popen(
                [sys.executable, str(self.root / "runner.py"), "--bot-id", bot_id, "--data-dir", str(self.data_dir)],
                cwd=str(self.root), env=env, stdin=subprocess.DEVNULL, stdout=None, stderr=None, start_new_session=True,
            )
            self._processes[bot_id] = process
            self.registry.update_runtime(bot_id, "starting", process.pid)
            watcher = threading.Thread(target=self._watch, args=(bot_id, process), daemon=True, name=f"watch-{bot_id[:8]}")
            watcher.start()

    def _watch(self, bot_id: str, process: subprocess.Popen) -> None:
        code = process.wait()
        with self._lock:
            if self._processes.get(bot_id) is process:
                self._processes.pop(bot_id, None)
        record = self.registry.get(bot_id)
        if not record or str(record.get("status")) in {"deleted", "stopped"} or self._stopping.is_set():
            return
        self.registry.update_runtime(bot_id, "reconnecting", None, f"child exited with code {code}")
        # A stopped child is restarted only while its record remains active.
        if not self._stopping.wait(2.0):
            refreshed = self.registry.get(bot_id)
            if refreshed and str(refreshed.get("status")) not in {"deleted", "stopped"}:
                self.start_record(refreshed)

    def restore(self) -> None:
        for record in self.registry.all():
            if str(record.get("status")) not in {"deleted", "stopped"}:
                self.start_record(record)

    def stop_records(self, records: list[dict]) -> None:
        for record in records:
            bot_id = str(record.get("id") or "")
            with self._lock:
                process = self._processes.pop(bot_id, None)
            if process and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                except Exception:
                    pass

    def shutdown(self) -> None:
        self._stopping.set()
        with self._lock:
            records = [{"id": key} for key in self._processes]
        self.stop_records(records)

    def _help(self, username: str) -> str:
        if self.registry.language(username) == "en":
            return (
                "🛠️ S-Boot Server — send these commands in this private chat\n"
                "Add a controller bot: bot_username@bot_password@room_name\n"
                "Example: ControlBot@password123@MainRoom\n"
                "Add a silent bot: hb@bot_username@bot_password@room_name\n"
                "Example: hb@SilentBot@password123@MainRoom\n"
                "del@room / delall@room — remove bots\n"
                "bots / room@room — server status\n"
                "master@user@room / delmaster@user@room / masters@room\n"
                "The controller accepts only administration, protection, invitations and lists.\n"
                "Keep bot passwords private. Change language: lang@ar / lang@en."
            )
        return (
            "🛠️ سيرفر S-Boot — أرسل الأوامر التالية في هذا الخاص\n"
            "إضافة بوت متحكم: اسم_البوت@كلمة_مروره@اسم_الغرفة\n"
            "مثال: ControlBot@password123@MainRoom\n"
            "إضافة بوت صامت: hb@اسم_البوت@كلمة_مروره@اسم_الغرفة\n"
            "مثال: hb@SilentBot@password123@MainRoom\n"
            "del@الغرفة / delall@الغرفة — حذف البوتات\n"
            "bots / room@الغرفة — عرض الحالة\n"
            "master@اسم@الغرفة / delmaster@اسم@الغرفة / masters@الغرفة\n"
            "البوت المتحكم للإدارة والحماية والدعوات وعرض القوائم فقط.\n"
            "لا ترسل كلمات المرور في مجموعة. تغيير اللغة: lang@ar / lang@en."
        )

    @staticmethod
    def _parse_add(payload: str, role: str, sender: str) -> BotSpec:
        if "@" not in payload:
            raise RegistryError("الصيغة: اسم_البوت@كلمة_المرور@اسم_الغرفة")
        account_payload, room = payload.rsplit("@", 1)
        if "@" not in account_payload:
            raise RegistryError("الصيغة: اسم_البوت@كلمة_المرور@اسم_الغرفة")
        username, password = account_payload.split("@", 1)
        return BotSpec(username=username, password=password, room=room, role=role, master=sender)

    def _format_bots(self, username: str) -> str:
        bots = self.registry.visible_bots(username)
        if not bots:
            return "🤖 لا توجد بوتات تحت تحكمك." if self.registry.language(username) != "en" else "🤖 You have no managed bots."
        labels = []
        for item in bots:
            role = "متحكم" if item.get("role") == "controller" else "صامت"
            labels.append(f"• @{item.get('username')} — {role} — {item.get('room')} — {item.get('status')}")
        return "🤖 بوتاتك:\n" + "\n".join(labels)

    def _format_room(self, sender: str, room: str) -> str:
        visible = [item for item in self.registry.visible_bots(sender) if normalize(item.get("room")) == normalize(room)]
        if not visible:
            raise RegistryError("الغرفة غير مسجلة تحت تحكمك")
        master = visible[0].get("master")
        lines = [f"🏠 الغرفة: {visible[0].get('room')}", f"👑 الماستر: @{master}", f"🤖 البوتات: {len(visible)}"]
        lines.extend(f"• @{item.get('username')} — {item.get('role')} — {item.get('status')}" for item in visible)
        return "\n".join(lines)

    def handle_command(self, sender: str, text: str) -> str:
        text = str(text or "").strip()
        low = text.casefold()
        if not text:
            return "اكتب help لعرض أوامر سيرفر S-Boot."
        try:
            if low in {"help", "مساعدة", "الاوامر", "الأوامر"}:
                return self._help(sender)
            if low == "bots":
                return self._format_bots(sender)
            if low.startswith("room@"):
                return self._format_room(sender, text.split("@", 1)[1].strip())
            if low.startswith("hb@"):
                record = self.registry.create(self._parse_add(text[3:], "silent", sender))
                self.start_record(record)
                return f"✅ تمت إضافة @{record['username']} كبوت صامت لغرفة {record['room']}.\n📌 سيتم تحديث حالته إلى «بوت صامت» عند الاتصال."
            if low.startswith(("بوتصامت@", "صامت@")):
                payload = text.split("@", 1)[1]
                record = self.registry.create(self._parse_add(payload, "silent", sender))
                self.start_record(record)
                return f"✅ تمت إضافة @{record['username']} كبوت صامت لغرفة {record['room']}."
            if "@" in text and not low.startswith(("del@", "delall@", "clean@", "master@", "delmaster@", "masters@", "lang@")):
                record = self.registry.create(self._parse_add(text, "controller", sender))
                self.start_record(record)
                return (
                    f"✅ تمت إضافة @{record['username']} كبوت متحكم لغرفة {record['room']}.\n"
                    "🛡️ الأوامر المتاحة له: الإدارة والحماية والدعوات وعرض القوائم فقط.\n"
                    "📌 سيتم تطبيق الحالة الملوّنة عند اتصال البوت."
                )
            if low.startswith("delall@") or low.startswith("clean@"):
                room = text.split("@", 1)[1].strip()
                removed = self.registry.delete(room, sender, all_bots=True)
                self.stop_records(removed)
                return f"✅ تم حذف {len(removed)} بوت من الغرفة {room}."
            if low.startswith("del@"):
                room = text.split("@", 1)[1].strip()
                removed = self.registry.delete(room, sender, all_bots=False)
                self.stop_records(removed)
                return f"✅ تم حذف {len(removed)} بوت متحكم من الغرفة {room}."
            if low.startswith("master@"):
                _, target, room = text.split("@", 2)
                self.registry.add_delegate(room, sender, target)
                return f"✅ تمت إضافة @{target.strip().lstrip('@')} كماستر للغرفة {room.strip()}."
            if low.startswith("delmaster@"):
                _, target, room = text.split("@", 2)
                self.registry.remove_delegate(room, sender, target)
                return f"✅ تمت إزالة @{target.strip().lstrip('@')} من ماسترات الغرفة {room.strip()}."
            if low.startswith("masters@"):
                room = text.split("@", 1)[1].strip()
                primary, delegates = self.registry.masters(room, sender)
                values = [f"👑 الماستر الأساسي: @{primary}"] + [f"• @{item}" for item in delegates]
                return "\n".join(values)
            if low.startswith("lang@"):
                value = text.split("@", 1)[1].strip().casefold()
                if value in {"ar", "عربي", "العربية"}:
                    self.registry.set_pending_language(sender)
                    self.registry.choose_language(sender, "1")
                    return "✅ تم اختيار العربية."
                if value in {"en", "english", "انجليزي", "الانجليزية"}:
                    self.registry.set_pending_language(sender)
                    self.registry.choose_language(sender, "2")
                    return "✅ English selected."
                return "استخدم: lang@ar أو lang@en"
        except (RegistryError, ValueError) as exc:
            return f"❌ {exc}"
        return "❓ أمر غير معروف. اكتب help."

    def health(self) -> dict:
        records = self.registry.all()
        active = [item for item in records if str(item.get("status")) not in {"deleted", "stopped"}]
        online = [item for item in active if item.get("status") == "online"]
        return {"status": "online", "service": "talkin-sboot", "active_bots": len(active), "online_bots": len(online)}
