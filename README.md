# Talkin S-Boot Server

Arabic documentation and deployment steps are in [README_AR.md](README_AR.md).

A standalone Talkin/ChatP bot-entry server for controller and silent accounts. It preserves account avatars, updates colored profile statuses, creates and reuses its encryption key on first startup, and accepts incoming friend requests through Talkin's native protocol without Supabase. Controller bots provide paged admin help, moderation, room-specific invitation text, complete owner/admin/member invite collection, single-message invite history, queued all-room bans, and an opt-in six-ball cricket match with 1–4 players per room, rotating player turns, and a solo opponent handled by the room's controller bot. The transparent game icons are each under 100 KB. See `README_AR.md` for commands and deployment details.
