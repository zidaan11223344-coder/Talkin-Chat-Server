"""Shared administrative actions consumed by the controller bot in each room."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from state import JsonState, normalize


def _blank():
    return {"next_id": 0, "actions": []}


class RoomActionQueue:
    def __init__(self, root: str | Path):
        path = Path(root)
        if path.suffix.lower() != ".json":
            path = path / "room_admin_actions.json"
        self.state = JsonState(path, _blank)

    def enqueue(self, target: str, rooms: list[str], requester: str, operation: str = "ban") -> tuple[int, int]:
        target_name = str(target or "").strip().lstrip("@")
        targets: dict[str, str] = {}
        for room in rooms:
            name = str(room or "").strip()
            if normalize(name):
                targets.setdefault(normalize(name), name)
        if not target_name or not targets:
            raise ValueError("المستخدم أو غرف السيرفر غير محدد")

        def mutate(data: dict[str, Any]) -> tuple[int, int]:
            data["next_id"] = int(data.get("next_id", 0)) + 1
            action_id = int(data["next_id"])
            data.setdefault("actions", []).append({
                "id": action_id,
                "operation": operation,
                "target": target_name,
                "rooms": targets,
                "requester": str(requester or ""),
                "done": [],
                "retry_at": {},
                "created_at": int(time.time()),
            })
            data["actions"] = data["actions"][-500:]
            return action_id, len(targets)

        return self.state.mutate(mutate)

    def pending_for_room(self, room: str) -> list[dict[str, Any]]:
        key = normalize(room)
        now = time.time()
        data = self.state.load()
        out = []
        for item in data.get("actions", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            retry_at = (item.get("retry_at") or {}).get(key, 0)
            if key in (item.get("rooms") or {}) and key not in set(item.get("done") or []) and float(retry_at or 0) <= now:
                out.append(dict(item))
        return out

    def defer(self, action_id: int, room: str, seconds: float = 30.0) -> None:
        key = normalize(room)
        def mutate(data: dict[str, Any]) -> None:
            for item in data.get("actions", []):
                if isinstance(item, dict) and int(item.get("id", 0)) == int(action_id):
                    item.setdefault("retry_at", {})[key] = time.time() + max(1.0, float(seconds))
                    break
        self.state.mutate(mutate)

    def mark_done(self, action_id: int, room: str) -> None:
        key = normalize(room)
        def mutate(data: dict[str, Any]) -> None:
            for item in data.get("actions", []):
                if isinstance(item, dict) and int(item.get("id", 0)) == int(action_id):
                    done = list(item.get("done") or [])
                    if key not in done:
                        done.append(key)
                    item["done"] = done
                    (item.get("retry_at") or {}).pop(key, None)
                    break
        self.state.mutate(mutate)
