"""Talkin runtime for the central S-Boot entry account."""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from friendships import (
    FRIEND_REQUESTS_HANDLER_ID,
    WELCOME_TEXT,
    accept_friend_query,
    request_friendships_query,
    request_usernames,
)
from vendor.talkin_runtime import TalkinBot, decode_result_message


class ControlAccountBot(TalkinBot):
    """Receives only private S-Boot server commands; it never joins a room."""

    def __init__(self, service):
        self.service = service
        self._friend_poll_lock = threading.Lock()
        self._friend_poll_at = 0.0
        self._accepted_friends = set()
        super().__init__()

    def poll_friend_requests(self) -> bool:
        """Poll incoming requests through Talkin's own WebSocket protocol."""
        with self._friend_poll_lock:
            if not getattr(self, "ws", None):
                return False
            interval = max(2.0, float(os.getenv("FRIEND_POLL_SECONDS", "10")))
            now = time.monotonic()
            if now - self._friend_poll_at < interval:
                return False
            try:
                self.send_query(request_friendships_query())
                self._friend_poll_at = now
                self.log("[FRIENDS] requested pending friend list through Talkin")
                return True
            except Exception as exc:
                self.log("[FRIENDS] native request poll failed", repr(exc))
                return False

    def _accept_friend_requests(self, result: dict) -> int:
        accepted = 0
        for username in request_usernames(result):
            key = username.casefold()
            if key in self._accepted_friends:
                continue
            try:
                # The Android app uses profile_update / accept_friend and puts
                # the requester's username in Query.value (not a REST/Supabase
                # friendship table).
                self.send_query(accept_friend_query(username))
                self._accepted_friends.add(key)
                self.service.registry.set_pending_language(username)
                try:
                    self.send_private_text(username, WELCOME_TEXT)
                except Exception as exc:
                    self.log("[FRIENDS] accepted but welcome delivery failed", username, repr(exc))
                self.log("[FRIENDS] accepted through Talkin native protocol", username)
                accepted += 1
            except Exception as exc:
                self.log("[FRIENDS] native acceptance failed", username, repr(exc))
        return accepted

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
            if int(result.get("handler_id", 0) or 0) == FRIEND_REQUESTS_HANDLER_ID:
                self._accept_friend_requests(result)
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
