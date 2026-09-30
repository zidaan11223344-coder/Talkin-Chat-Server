"""Talkin runtime for the central S-Boot entry account."""
from __future__ import annotations

from typing import Any

from friendships import WELCOME_TEXT
from vendor.talkin_runtime import TalkinBot, decode_result_message


class ControlAccountBot(TalkinBot):
    """Receives only private S-Boot server commands; it never joins a room."""

    def __init__(self, service):
        self.service = service
        super().__init__()

    def welcome_friend(self, username: str) -> bool:
        self.service.registry.set_pending_language(username)
        if not getattr(self, "ws", None):
            self.log("[S-BOOT] friend accepted; welcome queued until the account reconnects", username)
            return False
        try:
            return bool(self.send_private_text(username, WELCOME_TEXT))
        except Exception as exc:
            self.log("[S-BOOT] welcome delivery failed", username, repr(exc))
            return False

    def _process_message(self, ws: Any, message: bytes) -> None:
        try:
            result = decode_result_message(message)
            chat = result.get("chat_message") or {}
            sender = str(chat.get(3, "") or "").strip().lstrip("@")
            body = str(chat.get(5, "") or "").strip()
            if sender and body:
                chosen = self.service.registry.choose_language(sender, body)
                if chosen == "ar":
                    reply = "✅ تم اختيار العربية.\n📌 أرسل help لعرض أوامر سيرفر S-Boot."
                elif chosen == "en":
                    reply = "✅ English selected.\n📌 Send help to view the S-Boot server commands."
                else:
                    reply = self.service.handle_command(sender, body)
                if reply:
                    self.send_private_text(sender, reply)
            if result.get("uid"):
                self.ack(str(result["uid"]))
        except Exception as exc:
            self.last_error = str(exc)
            self.log("[S-BOOT] control message processing failed", repr(exc))
