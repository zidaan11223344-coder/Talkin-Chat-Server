"""Supabase-backed friendship acceptance for the S-Boot server account."""
from __future__ import annotations

import threading
import time
from typing import Callable


WELCOME_TEXT = "مرحبا بك في سيرفر بوتات s-boot\n\nلاختيار العربية أرسل 1\nلاختيار الإنجليزية أرسل 2"


class FriendshipWatcher:
    """Accept incoming Talkin friendship requests when the app exposes the shared table."""

    def __init__(self, db_client, server_username: str, on_accept: Callable[[str], None], log: Callable[..., None] = print):
        self.db = db_client
        self.server_username = str(server_username or "").strip().lstrip("@")
        self.on_accept = on_accept
        self.log = log
        self._server_id = ""
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _resolve_server_id(self) -> str:
        if self._server_id:
            return self._server_id
        if not self.db or not self.server_username:
            return ""
        try:
            rows = self.db.table("profiles").select("id,username").eq("username", self.server_username).limit(1).execute().data or []
            if not rows:
                rows = self.db.table("profiles").select("id,username").ilike("username", self.server_username).limit(1).execute().data or []
            if rows:
                self._server_id = str(rows[0].get("id") or "")
        except Exception as exc:
            self.log("[FRIENDS] profile lookup failed", repr(exc))
        return self._server_id

    def accept_once(self) -> int:
        """Accept all pending requests and notify only after the update succeeds."""
        server_id = self._resolve_server_id()
        if not server_id:
            return 0
        accepted = 0
        try:
            requests = (
                self.db.table("friendships")
                .select("id,requester_id,addressee_id,status")
                .eq("addressee_id", server_id)
                .eq("status", "pending")
                .limit(100)
                .execute().data
                or []
            )
        except Exception as exc:
            self.log("[FRIENDS] pending request lookup failed", repr(exc))
            return 0
        for request in requests:
            request_id = str(request.get("id") or "")
            requester_id = str(request.get("requester_id") or "")
            if not request_id or not requester_id:
                continue
            try:
                updated = (
                    self.db.table("friendships")
                    .update({"status": "accepted"})
                    .eq("id", request_id)
                    .eq("status", "pending")
                    .execute().data
                )
                # Supabase may return an empty data array under RLS even when
                # the update completed; the conditional update remains safe.
                if updated is None:
                    continue
                profiles = self.db.table("profiles").select("username").eq("id", requester_id).limit(1).execute().data or []
                username = str((profiles[0] if profiles else {}).get("username") or "").strip().lstrip("@")
                if username:
                    self.on_accept(username)
                    accepted += 1
            except Exception as exc:
                self.log("[FRIENDS] request acceptance failed", request_id, repr(exc))
        return accepted

    def run(self, seconds: float = 1.0) -> None:
        while not self._stop.wait(max(0.25, float(seconds))):
            self.accept_once()
