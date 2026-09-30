"""Native Talkin friend-request protocol helpers; no Supabase is required."""
from __future__ import annotations

from typing import Any

from vendor.talkin_runtime import encode_query

# Verified against Talkinchat 5.8.3 Android app:
# RequestsActivity sends profile_update/send_requests with an empty value;
# the friend request row's accept action sends profile_update/accept_friend
# with the requester's username as Query.value.
FRIEND_REQUESTS_HANDLER_ID = 17
WELCOME_TEXT = "مرحبا بك في سيرفر بوتات s-boot\n\nلاختيار العربية أرسل 1\nلاختيار الإنجليزية أرسل 2"


def request_friendships_query() -> bytes:
    """Ask Talkin for the account's pending incoming friend requests."""
    return encode_query("profile_update", type_="send_requests", value="")


def accept_friend_query(username: str) -> bytes:
    """Create the same accept_friend packet sent by the Talkin Android app."""
    name = str(username or "").strip().lstrip("@")
    if not name:
        raise ValueError("username is required")
    return encode_query("profile_update", type_="accept_friend", value=name)


def request_usernames(result: dict[str, Any]) -> list[str]:
    """Extract usernames only from Talkin's friend-requests handler response."""
    try:
        handler_id = int(result.get("handler_id", 0) or 0)
    except (TypeError, ValueError):
        return []
    if handler_id != FRIEND_REQUESTS_HANDLER_ID:
        return []
    users = result.get("users") or []
    if isinstance(users, dict):
        users = [users]
    names: list[str] = []
    seen: set[str] = set()
    for user in users:
        if not isinstance(user, dict):
            continue
        value = user.get(1, user.get("username", ""))
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        username = str(value or "").strip().lstrip("@")
        key = username.casefold()
        if username and key not in seen:
            names.append(username)
            seen.add(key)
    return names
