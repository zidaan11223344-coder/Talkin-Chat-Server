# Vendored Talkin Runtime

`vendor/talkin_runtime.py` is copied from the user-selected repository:

- Source repository: `zidaan11223344-coder/Talkin2`
- Source revision: `9f3ff92`
- Source file: `bot.py`

It is retained as the Talkin/ChatP transport implementation. The S-Boot service adds `restricted_runtime.py`, which dispatches only the requested controller features: administration, protection, invitations, and lists.

## Native Friend Acceptance

Incoming friend requests use the Talkinchat 5.8.3 Android app's own protocol: `profile_update` with type `send_requests` to fetch pending requests, and `profile_update` with type `accept_friend` and the requester's username in the Query `value` field. These actions were verified from the app's request screen and accept button; friendship acceptance does not query or update Supabase.

Protocol-reference sources:

- [Talkinchat 5.8.3 Android package page](https://apkpure.com/migbuzz-chat-rooms/net.chatp/download/5.8.3) — package/version reference used to inspect the request and accept actions.
- [Talkinchat official website](https://talkinchat.com/) — product reference.
