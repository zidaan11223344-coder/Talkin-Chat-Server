"""Persistent, cross-room cricket matches shared by S-Boot controller bots."""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from state import JsonState, normalize


ASSET_FILES = tuple(f"cricket_ball_{number}.png" for number in range(1, 7)) + (
    "cricket_duck.png",
    "cricket_hattrick.png",
)


def _blank() -> dict[str, Any]:
    return {"enabled": False, "enabled_rooms": {}, "next_event_id": 0, "match": None, "events": []}


def _key(room: str) -> str:
    return normalize(room)


class CricketGame:
    """File-locked state machine; each controller bot delivers events to its room."""

    MIN_ROOMS = 2
    MAX_ROOMS = 8
    BALLS_PER_INNINGS = 6
    WICKETS_PER_INNINGS = 3
    EVENT_HISTORY = 5000

    def __init__(self, root: str | Path):
        path = Path(root)
        if path.suffix.lower() != ".json":
            path = path / "cricket_state.json"
        self.state = JsonState(path, _blank)

    @staticmethod
    def _participants(match: dict[str, Any]) -> list[dict[str, str]]:
        return [x for x in match.get("rooms", []) if isinstance(x, dict) and x.get("key") and x.get("name")]

    def _emit(self, data: dict[str, Any], rooms: list[dict[str, str]], text: str, images: tuple[str, ...] = ()) -> None:
        events = data.setdefault("events", [])
        for participant in rooms:
            data["next_event_id"] = int(data.get("next_event_id", 0)) + 1
            events.append({
                "id": data["next_event_id"],
                "room": participant["name"],
                "room_key": participant["key"],
                "text": str(text),
                "images": [name for name in images if name in ASSET_FILES],
                "created_at": time.time(),
            })
        if len(events) > self.EVENT_HISTORY:
            del events[:-self.EVENT_HISTORY]

    def enabled(self, room: str) -> bool:
        data = self.state.load()
        return bool(data.get("enabled", bool(data.get("enabled_rooms") or {})))

    def set_enabled(self, room: str, enabled: bool) -> str:
        room_name, room_key = str(room or "").strip(), _key(room)
        if not room_key:
            return "❌ اسم الغرفة غير صالح."

        def mutate(data: dict[str, Any]) -> str:
            data["enabled"] = bool(enabled)
            data["enabled_rooms"] = {}
            match = data.get("match")
            if enabled:
                return f"✅ تم تشغيل الكركيت على مستوى السيرفر من غرفة {room_name}. يمكن لأي غرفة بوت متحكم الانضمام. اكتب كركيت 2 لفتح مباراة."
            participants = self._participants(match or {}) if isinstance(match, dict) else []
            if isinstance(match, dict):
                other_rooms = [item for item in participants if item["key"] != room_key]
                self._emit(data, other_rooms, f"⛔ أوقفت غرفة {room_name} اللعبة؛ أُلغيت المباراة.")
                data["match"] = None
            return f"⛔ تم إيقاف الكركيت على مستوى السيرفر من غرفة {room_name}."

        return self.state.mutate(mutate)

    def start(self, room: str, room_count: int) -> str | None:
        room_name, room_key = str(room or "").strip(), _key(room)
        try:
            count = int(room_count)
        except (TypeError, ValueError):
            return "❌ اكتب عدد الغرف المشاركة من 2 إلى 8، مثال: كركيت 2."
        if not self.MIN_ROOMS <= count <= self.MAX_ROOMS:
            return "❌ عدد الغرف المشاركة يجب أن يكون من 2 إلى 8."

        def mutate(data: dict[str, Any]) -> str | None:
            if not bool(data.get("enabled", bool(data.get("enabled_rooms") or {}))):
                return "⛔ اللعبة متوقفة على مستوى السيرفر. شغّلها بالأمر .cr 1 من أي غرفة متحكمة."
            if isinstance(data.get("match"), dict):
                return "⏳ توجد مباراة مفتوحة بالفعل؛ اكتب join من غرفة أخرى أو انتظر انتهاء المباراة."
            participant = {"key": room_key, "name": room_name}
            match = {
                "id": uuid.uuid4().hex,
                "stage": "lobby",
                "created_at": time.time(),
                "target_rooms": count,
                "rooms": [participant],
                "teams": {},
                "innings": 1,
                "batting_team": "attack",
                "balls": 0,
                "scores": {"attack": 0, "defense": 0},
                "wickets": {"attack": 0, "defense": 0},
                "wicket_streak": 0,
                "choices": {},
            }
            data["match"] = match
            self._emit(data, [participant],
                       f"🏏 فُتحت لعبة الكركيت في غرفة {room_name}.\n"
                       f"👥 المطلوب: {count} غرف. انتظر انضمام الغرف الأخرى بكتابة join أو انضمام.\n"
                       "بعد اكتمال العدد يختار كل فريق: 1 هجوم أو 2 دفاع.")
            return None

        return self.state.mutate(mutate)

    def join(self, room: str) -> str | None:
        room_name, room_key = str(room or "").strip(), _key(room)

        def mutate(data: dict[str, Any]) -> str | None:
            if not bool(data.get("enabled", bool(data.get("enabled_rooms") or {}))):
                return "⛔ شغّل الكركيت على مستوى السيرفر بالأمر .cr 1 أولاً، ثم اكتب join."
            match = data.get("match")
            if not isinstance(match, dict) or match.get("stage") != "lobby":
                return "📭 لا توجد لعبة تنتظر الانضمام الآن."
            participants = self._participants(match)
            if any(x["key"] == room_key for x in participants):
                return "✅ هذه الغرفة منضمة بالفعل إلى اللعبة."
            if len(participants) >= int(match.get("target_rooms", 0)):
                return "⛔ اكتمل عدد الغرف المطلوبة لهذه المباراة."
            participant = {"key": room_key, "name": room_name}
            participants.append(participant)
            match["rooms"] = participants
            full = len(participants) >= int(match.get("target_rooms", 0))
            if full:
                match["stage"] = "teams"
                text = (
                    f"✅ انضمت غرفة {room_name} واكتمل العدد ({len(participants)}/{match['target_rooms']}).\n"
                    "⚔️ كل غرفة تختار فريقها: أرسل 1 للهجوم أو 2 للدفاع.\n"
                    "يجب أن يكون هناك فريق هجوم وفريق دفاع."
                )
            else:
                text = f"✅ انضمت غرفة {room_name} ({len(participants)}/{match['target_rooms']}). بانتظار بقية الغرف؛ اكتبوا join."
            self._emit(data, participants, text)
            return None

        return self.state.mutate(mutate)

    def choose_team(self, room: str, team: str) -> str | None:
        room_key = _key(room)
        team = "attack" if str(team).casefold() in {"attack", "1", "هجوم"} else "defense"
        label = "الهجوم" if team == "attack" else "الدفاع"

        def mutate(data: dict[str, Any]) -> str | None:
            match = data.get("match")
            if not isinstance(match, dict) or match.get("stage") != "teams":
                return "📭 اللعبة لا تنتظر اختيار الفرق الآن."
            participants = self._participants(match)
            participant = next((x for x in participants if x["key"] == room_key), None)
            if participant is None:
                return "⛔ هذه الغرفة ليست مشاركة في المباراة. اكتب join قبل اختيار الفريق."
            match.setdefault("teams", {})[room_key] = team
            teams = match["teams"]
            both_sides = "attack" in teams.values() and "defense" in teams.values()
            everyone_chose = all(x["key"] in teams for x in participants)
            if both_sides and everyone_chose:
                match["stage"] = "live"
                match["innings"] = 1
                match["batting_team"] = "attack"
                match["balls"] = 0
                match["wicket_streak"] = 0
                self._emit(
                    data,
                    participants,
                    "🏏 بدأت المباراة! فريق الهجوم يضرب أولاً.\n"
                    "🎯 في كل كرة: فريق الهجوم يرسل رقم 0–6، وفريق الدفاع يخمّن 0–6.\n"
                    "إذا تطابق الرقمان يخرج اللاعب؛ وإذا اختلفا تُحسب نقاط الهجوم. أرسلوا الرقم الآن.",
                )
            else:
                missing = match["target_rooms"] - len(participants)
                if missing:
                    text = f"✅ اختارت غرفة {participant['name']} فريق {label}. ما زال ينقص {missing} غرفة للانضمام."
                elif both_sides:
                    text = f"✅ اختارت غرفة {participant['name']} فريق {label}. بانتظار اختيار بقية الغرف."
                else:
                    text = f"✅ اختارت غرفة {participant['name']} فريق {label}. يجب اختيار الفريق الآخر أيضًا؛ يمكن تغيير الاختيار بإرسال 1 أو 2."
                self._emit(data, participants, text)
            return None

        return self.state.mutate(mutate)

    @staticmethod
    def _team_label(team: str) -> str:
        return "الهجوم" if team == "attack" else "الدفاع"

    def submit_ball(self, room: str, sender: str, number: int) -> str | None:
        room_key = _key(room)
        try:
            value = int(number)
        except (TypeError, ValueError):
            return "❌ اختر رقمًا من 0 إلى 6."
        if not 0 <= value <= 6:
            return "❌ اختر رقمًا من 0 إلى 6."

        def mutate(data: dict[str, Any]) -> str | None:
            match = data.get("match")
            if not isinstance(match, dict) or match.get("stage") != "live":
                return "📭 لا توجد كرة تنتظر الاختيارات الآن."
            participants = self._participants(match)
            participant = next((x for x in participants if x["key"] == room_key), None)
            if participant is None:
                return "⛔ هذه الغرفة ليست مشاركة في المباراة."
            team = (match.get("teams") or {}).get(room_key)
            batting = str(match.get("batting_team") or "attack")
            side = "bat" if team == batting else "bowl"
            choices = match.setdefault("choices", {})
            existing = choices.get(side)
            if existing and existing.get("room_key") != room_key:
                return "⏳ سجّلت غرفة أخرى من فريقك اختيار هذه الكرة بالفعل."
            choices[side] = {"value": value, "room_key": room_key, "room": participant["name"], "sender": str(sender or "")}
            if not (choices.get("bat") and choices.get("bowl")):
                side_label = "الهجوم" if side == "bat" else "الدفاع"
                self._emit(data, [participant], f"✅ سجّل @{sender} الرقم {value} لفريق {side_label}. بانتظار الفريق الآخر.")
                return None

            bat_choice, bowl_choice = choices["bat"], choices["bowl"]
            bat_value, bowl_value = int(bat_choice["value"]), int(bowl_choice["value"])
            ball_no = int(match.get("balls", 0)) + 1
            wickets = match.setdefault("wickets", {"attack": 0, "defense": 0})
            scores = match.setdefault("scores", {"attack": 0, "defense": 0})
            images = [f"cricket_ball_{ball_no}.png"]
            if bat_value == bowl_value:
                wickets[batting] = int(wickets.get(batting, 0)) + 1
                match["wicket_streak"] = int(match.get("wicket_streak", 0)) + 1
                outcome = "ويكيت! خرج لاعب الهجوم."
                if ball_no == 1 and int(wickets[batting]) == 1:
                    outcome += " 🦆 دَك من أول كرة!"
                    images.append("cricket_duck.png")
                if int(match["wicket_streak"]) == 3:
                    outcome += " 🔥 هاتريك — ثلاث ويكيت متتالية!"
                    images.append("cricket_hattrick.png")
            else:
                match["wicket_streak"] = 0
                scores[batting] = int(scores.get(batting, 0)) + bat_value
                outcome = f"سجّل فريق الهجوم {bat_value} نقطة." if bat_value else "كرة بلا نقاط؛ لا يوجد ويكيت."

            match["balls"] = ball_no
            match["choices"] = {}
            board = (
                f"\n📊 النتيجة — الهجوم: {scores.get('attack', 0)} | الدفاع: {scores.get('defense', 0)}"
                f"\n🚫 الويكيت: {wickets.get(batting, 0)}/{self.WICKETS_PER_INNINGS}"
            )
            text = (
                f"🏏 الكرة {ball_no}/{self.BALLS_PER_INNINGS}\n"
                f"⚔️ @{bat_choice.get('sender') or bat_choice['room']} اختار {bat_value} | "
                f"🛡️ @{bowl_choice.get('sender') or bowl_choice['room']} خمّن {bowl_value}\n"
                f"{outcome}{board}"
            )
            self._emit(data, participants, text, tuple(images))

            attack_score = int(scores.get("attack", 0))
            defense_score = int(scores.get("defense", 0))
            if int(match.get("innings", 1)) == 2 and defense_score > attack_score:
                self._finish(data, match, participants)
                return None

            innings_over = (
                ball_no >= self.BALLS_PER_INNINGS
                or int(wickets.get(batting, 0)) >= self.WICKETS_PER_INNINGS
            )
            if innings_over:
                if int(match.get("innings", 1)) == 1:
                    match["innings"] = 2
                    match["batting_team"] = "defense"
                    match["balls"] = 0
                    match["wickets"]["defense"] = 0
                    match["wicket_streak"] = 0
                    target = attack_score + 1
                    self._emit(
                        data,
                        participants,
                        f"🏁 انتهى الشوط الأول. هدف فريق الدفاع: {target} نقطة.\n"
                        "الآن يضرب فريق الدفاع؛ أرسلوا أرقامكم 0–6.",
                    )
                else:
                    self._finish(data, match, participants)
            return None

        return self.state.mutate(mutate)

    def _finish(self, data: dict[str, Any], match: dict[str, Any], participants: list[dict[str, str]]) -> None:
        scores = match.get("scores") or {}
        attack_score = int(scores.get("attack", 0))
        defense_score = int(scores.get("defense", 0))
        winner = "فريق الهجوم" if attack_score > defense_score else "فريق الدفاع" if defense_score > attack_score else "تعادل"
        self._emit(
            data,
            participants,
            "🏆 انتهت مباراة الكركيت\n━━━━━━━━━━━━\n"
            f"⚔️ نقاط الهجوم: {attack_score}\n🛡️ نقاط الدفاع: {defense_score}\n"
            f"👑 النتيجة: {winner}",
        )
        data["match"] = None

    def current(self) -> dict[str, Any] | None:
        data = self.state.load()
        match = data.get("match")
        return match if isinstance(match, dict) else None

    def latest_event_id(self, room: str) -> int:
        room_key = _key(room)
        data = self.state.load()
        events = data.get("events", [])
        return max((int(x.get("id", 0)) for x in events if isinstance(x, dict) and x.get("room_key") == room_key), default=0)

    def events_after(self, room: str, event_id: int) -> list[dict[str, Any]]:
        room_key = _key(room)
        data = self.state.load()
        return [dict(x) for x in data.get("events", []) if isinstance(x, dict) and x.get("room_key") == room_key and int(x.get("id", 0)) > int(event_id)]
