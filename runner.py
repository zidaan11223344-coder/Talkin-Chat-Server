"""Starts exactly one inserted Talkin child bot from encrypted registry state."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from registry import BotRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description="S-Boot Talkin child runner")
    parser.add_argument("--bot-id", required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    load_dotenv(override=False)
    key = os.getenv("STATE_ENCRYPTION_KEY", "")
    registry = BotRegistry(args.data_dir, key, os.getenv("SERVER_ADMIN_NAME", ""))
    record = registry.get(args.bot_id, include_password=True)
    if not record or record.get("status") == "deleted":
        return 0
    child_root = Path(args.data_dir) / "children" / str(record["id"])
    child_root.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "BOT_ID": str(record["username"]),
        "BOT_PWD": str(record["password"]),
        "BOT_MASTER": str(record["master"]),
        "GROUP_TO_JOIN": str(record["room"]),
        "BOT_ENTRY_TYPE": str(record["role"]),
        "BOT_FIRST_CONNECTION_STATUS": str(record["profile_status"]),
        "BOT_BASE_STATUS": str(record["profile_status"]),
        "BOT_DATA_DIR": str(child_root),
        "CRICKET_MEDIA_DIR": str(Path(args.data_dir).resolve() / "cricket_media"),
        "ASSET_HTTP_ENABLED": "0",
        "GITHUB_SYNC": "0",
        "AUTO_JOIN_ALL_ROOMS": "0",
        "RUNNING_AS_MASTER": "0",
        "MASTER_SERVICE_ENABLED": "0",
    })
    # Report room-join success/failure to the master unless explicitly disabled.
    os.environ.setdefault("BOT_JOIN_NOTIFY_MASTER", "1")
    print(
        f"[S-BOOT] saved credentials decrypted; starting {record['role']} bot for room {record['room']}",
        flush=True,
    )
    registry.update_runtime(record["id"], "starting", os.getpid())
    # Import after environment variables are ready: the Talkin runtime reads
    # all account settings at module import time.
    from restricted_runtime import RestrictedTalkinBot
    RestrictedTalkinBot(record["id"], args.data_dir).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
