"""Persistent cross-room cricket with player rosters and an optional S-Boot opponent."""
from __future__ import annotations

import random
import time
import uuid
from pathlib import Path
from typing import Any

from cricket_state import JsonState, normalize


ASSET_FILES = tuple(f"cricket_ball_{number}.png" for number in range(0, 7)) + (
    "cricket_duck.png",
    "cricket_hattrick.png",
)
BOT_TEAM_KEY = "__sboot_cricket_bot__"


def _blank() -> dict[str, Any]:
    return {
        "enabled": False,
        "enabled_rooms": {},
        "next_event_id": 0,
        "match": None,
        "events": [],
        "points": {},
        "wins": {},
    }


def _key(room: str) -> str:
    return normalize(room)


def _user_key(username: str) -> str:
    return normalize(str(username or "").strip().lstrip("@"))


class CricketGame:
    """File-locked state machine; controller bots deliver events to their own rooms."""

    MIN_PLAYERS = 1
    MAX_PLAYERS = 4
    ROOM_TEAMS = 2
    BALLS_PER_INNINGS = 6
    EVENT_HISTORY = 5000

    def __init__(self, root: str | Path):
        path = Path(root)
        if path.suffix.lower() != ".json":
            path = path / "cricket_state.json"
        self.state = JsonState(path, _blank)

    @staticmethod
    def _participants(match: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            item for item in match.get("rooms", [])
            if isinstance(item, dict) and item.get("key") and item.get("name")
        ]

    def _emit(self, data: dict[str, Any], rooms: list[dict[str, Any]], text: str, images: tuple[str, ...] = ()) -> None:
        events = data.setdefault("events", [])
        for participant in rooms:
            data["next_event_id"] = int(data.get("next_event_id", 0)) + 1
            events.append({
                "id": data["next_event_id"],
                "room": participant["name"],
                "room_key": participant["key"],
                "text": str(text),
                "images": [
                    name for name in images
                    if name in ASSET_FILES or str(name).startswith("cricket_result_")
                ],
                "created_at": time.time(),
            })
        if len(events) > self.EVENT_HISTORY:
            del events[:-self.EVENT_HISTORY]

    def _emit_room_messages(
        self,
        data: dict[str, Any],
        messages: dict[str, str],
        images: tuple[str, ...] = (),
    ) -> None:
        """Emit short, room-specific messages while keeping one shared match state."""
        events = data.setdefault("events", [])
        for room_key, text in messages.items():
            if not room_key or text is None:
                continue
            data["next_event_id"] = int(data.get("next_event_id", 0)) + 1
            events.append({
                "id": data["next_event_id"],
                "room": room_key,
                "room_key": _key(room_key),
                "text": str(text),
                "images": [
                    name for name in images
                    if name in ASSET_FILES or str(name).startswith("cricket_result_")
                ],
                "created_at": time.time(),
            })
        if len(events) > self.EVENT_HISTORY:
            del events[:-self.EVENT_HISTORY]

    def enabled(self, room: str = "") -> bool:
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
                return f"✅ تم تشغيل الكركيت على مستوى السيرفر من غرفة {room_name}."
            participants = self._participants(match) if isinstance(match, dict) else []
            if isinstance(match, dict):
                other_rooms = [item for item in participants if item["key"] != room_key]
                self._emit(data, other_rooms, f"⛔ أوقفت غرفة {room_name} اللعبة؛ أُلغيت المباراة.")
                data["match"] = None
            return f"⛔ تم إيقاف الكركيت على مستوى السيرفر من غرفة {room_name}."

        return self.state.mutate(mutate)

    @staticmethod
    def _new_match(room_name: str, room_key: str, stage: str, player_count: int | None = None) -> dict[str, Any]:
        match: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "stage": stage,
            "created_at": time.time(),
            "setup_room": room_key,
            "target_players": player_count,
            "rooms": [{"key": room_key, "name": room_name, "players": []}],
            "mode": "rooms",
            "teams": {},
            "innings": 1,
            "batting_team": "attack",
            "balls": 0,
            "scores": {"attack": 0, "defense": 0},
            "wickets": {"attack": 0, "defense": 0},
            "out_players": {"attack": [], "defense": []},
            "turns": {"attack": 0, "defense": 0},
            "wicket_streak": 0,
            "choices": {},
            "player_scores": {},
        }
        return match

    def begin_setup(self, room: str) -> str | None:
        room_name, room_key = str(room or "").strip(), _key(room)

        def mutate(data: dict[str, Any]) -> str | None:
            if not data.get("enabled"):
                return "⛔ شغّل اللعبة أولاً بالأمر .cr 1."
            if isinstance(data.get("match"), dict):
                return "⏳ توجد مباراة/قائمة انتظار مفتوحة بالفعل. أرسل Join للانضمام أو انتظر انتهائها."
            match = self._new_match(room_name, room_key, "setup")
            data["match"] = match
            self._emit(
                data,
                self._participants(match),
                "🏏 إعداد مباراة الكركيت\n"
                "اختر عدد اللاعبين داخل هذه الغرفة فقط:\n"
                "1️⃣ لاعب واحد\n2️⃣ لاعبان\n3️⃣ ثلاثة لاعبين\n4️⃣ أربعة لاعبين\n"
                "أرسل الرقم فقط، وبعدها كل لاعب يرسل Join في نفس الغرفة.\n"
                "🤖 عند اكتمال العدد يبدأ اللعب تلقائيًا ضد بوت S-Boot.",
            )
            return None

        return self.state.mutate(mutate)

    def select_player_count(self, room: str, player_count: int) -> str | None:
        room_key = _key(room)
        try:
            count = int(player_count)
        except (TypeError, ValueError):
            return "❌ اختر 1 أو 2 أو 3 أو 4 لاعبين لكل غرفة."
        if not self.MIN_PLAYERS <= count <= self.MAX_PLAYERS:
            return "❌ عدد اللاعبين في كل غرفة يجب أن يكون من 1 إلى 4."

        def mutate(data: dict[str, Any]) -> str | None:
            match = data.get("match")
            if not isinstance(match, dict) or match.get("stage") != "setup":
                return "📭 لا توجد لعبة تنتظر اختيار عدد اللاعبين."
            if room_key != str(match.get("setup_room") or ""):
                return "🔒 اختيار عدد اللاعبين متاح في غرفة بدء اللعبة فقط."
            match["target_players"] = count
            match["stage"] = "lobby"
            room_name = self._participants(match)[0]["name"]
            self._emit(
                data,
                self._participants(match),
                f"🏏 كركيت | الفريق: {count} لاعبين\n"
                "أرسل Join للانضمام.\n"
                "بعد اكتمال الفريق، تنضم الغرفة الثانية بـ Join.",
            )
            return None

        return self.state.mutate(mutate)

    def start(self, room: str, player_count: int) -> str | None:
        """Open a lobby directly; retained for the `cricket N` command."""
        room_name, room_key = str(room or "").strip(), _key(room)
        try:
            count = int(player_count)
        except (TypeError, ValueError):
            return "❌ اكتب عدد اللاعبين في كل غرفة من 1 إلى 4."
        if not self.MIN_PLAYERS <= count <= self.MAX_PLAYERS:
            return "❌ عدد اللاعبين في كل غرفة يجب أن يكون من 1 إلى 4."

        def mutate(data: dict[str, Any]) -> str | None:
            if not bool(data.get("enabled", bool(data.get("enabled_rooms") or {}))):
                return "⛔ اللعبة متوقفة على مستوى السيرفر. شغّلها بالأمر .cr 1 أولاً."
            if isinstance(data.get("match"), dict):
                return "⏳ توجد مباراة مفتوحة بالفعل؛ أرسل Join للانضمام أو انتظر انتهائها."
            match = self._new_match(room_name, room_key, "lobby", count)
            match["mode"] = "solo"
            data["match"] = match
            self._emit(
                data,
                self._participants(match),
                f"🏏 فُتحت مباراة الكركيت في {room_name} — المطلوب {count} لاعب(ين).\n"
                "👤 كل اللاعبين ينضمون من هذه الغرفة فقط بإرسال Join.\n"
                "🤖 عند اكتمال العدد تبدأ المباراة تلقائيًا ضد S-Boot.",
            )
            return None

        return self.state.mutate(mutate)

    def _maybe_start_teams(self, data: dict[str, Any], match: dict[str, Any]) -> bool:
        rooms = self._participants(match)
        target = int(match.get("target_players") or 0)
        if match.get("mode") == "solo":
            if len(rooms) == 1 and len(rooms[0].get("players", [])) == 1:
                match["stage"] = "teams"
                self._emit(
                    data,
                    rooms,
                    "🤖 انضم بوت S-Boot خصمًا لك. اختر دورك: 1 للهجوم أو 2 للدفاع؛ "
                    "والبوت يأخذ الدور الآخر تلقائيًا.",
                )
                return True
            return False
        if len(rooms) == self.ROOM_TEAMS and target > 0 and all(
            len(item.get("players", [])) >= target for item in rooms
        ):
            match["stage"] = "teams"
            self._emit(
                data,
                rooms,
                f"✅ اكتمل الفريقان ({target} لاعب(ين) في كل غرفة).\n"
                "تختار كل غرفة دورها: 1 للهجوم أو 2 للدفاع. يجب أن يكون هناك فريق من كل نوع.",
            )
            return True
        return False

    def join(self, room: str, sender: str = "") -> str | None:
        room_name, room_key = str(room or "").strip(), _key(room)
        username = str(sender or "").strip().lstrip("@")
        user_key = _user_key(username)
        if not user_key:
            return "❌ تعذر تحديد اسم اللاعب؛ أرسل Join من حسابك داخل الغرفة."

        def mutate(data: dict[str, Any]) -> str | None:
            if not data.get("enabled"):
                return "⛔ شغّل الكركيت على مستوى السيرفر بالأمر .cr 1 أولاً."
            match = data.get("match")
            if not isinstance(match, dict) or match.get("stage") != "lobby":
                return "📭 لا توجد قائمة لاعبين مفتوحة الآن."
            target = int(match.get("target_players") or 0)
            if target < self.MIN_PLAYERS or target > self.MAX_PLAYERS:
                return "⏳ انتظر اختيار عدد اللاعبين أولاً."
            participants = self._participants(match)
            if any(_user_key(player) == user_key for item in participants for player in item.get("players", [])):
                return f"✅ @{username} مسجل بالفعل في المباراة."
            participant = next((item for item in participants if item["key"] == room_key), None)
            if participant is None:
                if len(participants) >= self.ROOM_TEAMS:
                    return "⛔ اكتملت غرفتا المباراة."
                # The second team can join only after the starting team is complete.
                first_room = participants[0]
                if len(first_room.get("players", [])) < target:
                    return f"⏳ أكمل الفريق الأول {target} لاعبين أولاً."
                participant = {"key": room_key, "name": room_name, "players": []}
                match.setdefault("rooms", []).append(participant)
                participants = self._participants(match)
                self._emit(
                    data,
                    participants,
                    f"🔗 انضمت غرفة {room_name} للمباراة الجماعية.\n"
                    f"👥 المطلوب {target} لاعب(ين) في كل غرفة.\n"
                    "أرسل Join من لاعبي هذه الغرفة.",
                )
            players = participant.setdefault("players", [])
            if len(players) >= target:
                return f"⛔ اكتمل عدد اللاعبين ({target}) في هذه الغرفة."
            players.append(username)

            if len(participants) == 1:
                if len(players) < target:
                    self._emit(
                        data, [participant],
                        f"✅ انضم @{username}.\n👥 اكتمل {len(players)}/{target} لاعب في الغرفة.\n"
                        "🔗 للمباراة الجماعية: اجعل الغرفة الثانية ترسل Join، أو أكمل العدد هنا للعب ضد S-Boot.",
                    )
                else:
                    if match.get("mode") == "solo":
                        # Direct `cricket N` keeps the original one-room vs S-Boot flow.
                        match["mode"] = "solo"
                        match["teams"] = {room_key: "attack", BOT_TEAM_KEY: "defense"}
                        self._start_live(data, match, participants)
                    else:
                        # Setup mode remains open for a second room to join.
                        self._emit(
                            data, [participant],
                            f"🏏 اكتمل الفريق الأول: {target} لاعبين\n"
                            "🔗 الفريق الثاني يدخل من غرفته بإرسال Join.",
                        )
            elif len(participants) == self.ROOM_TEAMS:
                full = all(len(item.get("players", [])) >= target for item in participants)
                if full:
                    match["mode"] = "rooms"
                    match["stage"] = "teams"
                    self._emit(
                        data, participants,
                        f"🏏 اكتمل الفريقان: {target} لاعبين\n"
                        "🎯 الفريق الأول يختار: 1 هجوم أو 2 دفاع.\n"
                        "الفريق الثاني يُحدد تلقائيًا.",
                    )
                else:
                    self._emit(
                        data, [participant],
                        f"✅ انضم @{username}.\n👥 اكتمل {len(players)}/{target} لاعب في هذه الغرفة.\n"
                        "⏳ بانتظار اكتمال لاعبي الغرفة الأخرى.",
                    )
            return None

        return self.state.mutate(mutate)

    def play_bot(self, room: str, sender: str) -> str | None:
        room_name, room_key = str(room or "").strip(), _key(room)
        username = str(sender or "").strip().lstrip("@")
        user_key = _user_key(username)

        def mutate(data: dict[str, Any]) -> str | None:
            match = data.get("match")
            if not data.get("enabled"):
                return "⛔ شغّل الكركيت أولاً بالأمر .cr 1."
            if not isinstance(match, dict) or match.get("stage") != "lobby":
                return "📭 لا توجد مباراة تنتظر خصم البوت."
            if int(match.get("target_players") or 0) != 1:
                return "🤖 اللعب مع البوت متاح عند اختيار لاعب واحد لكل فريق فقط."
            rooms = self._participants(match)
            if len(rooms) != 1 or rooms[0]["key"] != room_key:
                return "⛔ خصم البوت متاح في غرفة بدء المباراة قبل انضمام غرفة أخرى."
            players = rooms[0].setdefault("players", [])
            if players and not any(_user_key(player) == user_key for player in players):
                return "⛔ يوجد لاعب مسجل بالفعل؛ أرسل Join بحسابه أو انتظر الغرفة الأخرى."
            if not players:
                if not user_key:
                    return "❌ تعذر تحديد اسم اللاعب."
                players.append(username)
            match["mode"] = "solo"
            self._maybe_start_teams(data, match)
            return None

        return self.state.mutate(mutate)

    def choose_team(self, room: str, team: str) -> str | None:
        room_key = _key(room)
        team_value = str(team or "").casefold().strip()
        if team_value in {"attack", "1", "هجوم"}:
            team_value = "attack"
        elif team_value in {"defense", "2", "دفاع"}:
            team_value = "defense"
        else:
            return "❌ اختر 1 للهجوم أو 2 للدفاع."

        def mutate(data: dict[str, Any]) -> str | None:
            match = data.get("match")
            if not isinstance(match, dict):
                return "📭 لا توجد مباراة."

            # Compatibility: a repeated team command after auto-assignment is harmless.
            if match.get("stage") == "live":
                assigned = (match.get("teams") or {}).get(room_key)
                if assigned == team_value:
                    return None
                return "⏳ بدأت المباراة بالفعل."

            if match.get("stage") != "teams":
                return "📭 اللعبة لا تنتظر اختيار الهجوم أو الدفاع."

            participants = self._participants(match)
            if len(participants) != 2:
                return "⏳ بانتظار انضمام الفريق الثاني."
            if room_key != str(match.get("setup_room") or ""):
                return "⏳ الفريق المشغّل للعبة هو من يختار الهجوم أو الدفاع."

            other = next(item for item in participants if item["key"] != room_key)
            teams = match.setdefault("teams", {})
            teams[room_key] = team_value
            teams[other["key"]] = "defense" if team_value == "attack" else "attack"
            self._start_live(data, match, participants)
            return None

        return self.state.mutate(mutate)

    @staticmethod
    def _team_label(team: str) -> str:
        return "الهجوم" if team == "attack" else "الدفاع"

    @staticmethod
    def _room_for_team(match: dict[str, Any], team: str) -> dict[str, Any] | None:
        for participant in CricketGame._participants(match):
            if (match.get("teams") or {}).get(participant["key"]) == team:
                return participant
        return None

    def _next_player(self, match: dict[str, Any], team: str, batting: bool) -> str:
        participant = self._room_for_team(match, team)
        if participant is None:
            return "🤖 بوت S-Boot"
        players = [str(item) for item in participant.get("players", []) if str(item).strip()]
        if not players:
            return participant["name"]
        turns = match.setdefault("turns", {"attack": 0, "defense": 0})
        start = int(turns.get(team, 0)) % len(players)
        out = {_user_key(item) for item in match.get("out_players", {}).get(team, [])} if batting else set()
        for offset in range(len(players)):
            candidate = players[(start + offset) % len(players)]
            if _user_key(candidate) not in out:
                return candidate
        return ""

    def _turn_prompt(self, match: dict[str, Any]) -> str:
        """Return a compact generic prompt (kept for older integrations)."""
        batting = str(match.get("batting_team") or "attack")
        bowling = "defense" if batting == "attack" else "attack"
        batter = self._next_player(match, batting, batting=True)
        bowler = self._next_player(match, bowling, batting=False)
        return f"🎯 دورك يا @{batter} — أرسل 0 إلى 6.\n🛡️ بعده دور @{bowler}."

    def _turn_messages(self, match: dict[str, Any]) -> dict[str, str]:
        batting = str(match.get("batting_team") or "attack")
        bowling = "defense" if batting == "attack" else "attack"
        batter = self._next_player(match, batting, batting=True)
        bowler = self._next_player(match, bowling, batting=False)
        messages: dict[str, str] = {}

        if match.get("mode") == "solo":
            human_key = next((key for key in (match.get("teams") or {}) if key != BOT_TEAM_KEY), None)
            human_team = str((match.get("teams") or {}).get(human_key or "", "attack"))
            human_room = self._room_for_team(match, human_team)
            if human_room:
                if batting == human_team:
                    messages[human_room["key"]] = f"🏏 الهجوم\n🎯 دورك يا @{batter}\nأرسل 0 إلى 6"
                else:
                    messages[human_room["key"]] = f"🛡️ الدفاع\n🎯 دورك يا @{bowler}\nأرسل 0 إلى 6"
            return messages

        bat_room = self._room_for_team(match, batting)
        bowl_room = self._room_for_team(match, bowling)
        if bat_room:
            messages[bat_room["key"]] = f"🏏 الهجوم\n🎯 دورك يا @{batter}\nأرسل 0 إلى 6"
        if bowl_room:
            messages[bowl_room["key"]] = "🛡️ الدفاع\n⏳ بانتظار ضربة المهاجم"
        return messages

    def _start_live(self, data: dict[str, Any], match: dict[str, Any], participants: list[dict[str, Any]]) -> None:
        match["stage"] = "live"
        match["innings"] = 1
        match["batting_team"] = str((match.get("teams") or {}).get(str(match.get("setup_room") or ""), "attack"))
        if match["batting_team"] not in {"attack", "defense"}:
            match["batting_team"] = "attack"
        match["balls"] = 0
        match["scores"] = {"attack": 0, "defense": 0}
        match["wickets"] = {"attack": 0, "defense": 0}
        match["out_players"] = {"attack": [], "defense": []}
        match["turns"] = {"attack": 0, "defense": 0}
        match["wicket_streak"] = 0
        match["choices"] = {}
        match["player_scores"] = {
            str(player).lstrip("@"): 0
            for item in participants
            for player in item.get("players", [])
            if str(player).strip()
        }
        start_text = "🏏 بدأت الكركيت\n" + self._team_label(match["batting_team"]) + " تبدأ الهجوم."
        if match.get("mode") == "solo":
            start_text += "\n🤖 بوت S-Boot"
        self._emit(data, participants, start_text)
        self._emit_room_messages(data, self._turn_messages(match))

    def _team_player_count(self, match: dict[str, Any], team: str) -> int:
        if team == BOT_TEAM_KEY:
            return 1
        participant = self._room_for_team(match, team)
        if participant:
            return max(1, len(participant.get("players", [])))
        if match.get("mode") == "solo":
            rooms = self._participants(match)
            return max(1, len(rooms[0].get("players", []))) if rooms else 1
        return 1

    def _finish(self, data: dict[str, Any], match: dict[str, Any], participants: list[dict[str, Any]]) -> None:
        scores = match.get("scores") or {}
        attack_score = int(scores.get("attack", 0))
        defense_score = int(scores.get("defense", 0))
        winner_team = "attack" if attack_score > defense_score else "defense" if defense_score > attack_score else "tie"
        teams = match.get("teams") or {}
        player_scores = {str(k).lstrip("@"): int(v) for k, v in (match.get("player_scores") or {}).items()}
        prize = 200_000

        team_rooms: dict[str, dict[str, Any] | None] = {
            "attack": self._room_for_team(match, "attack"),
            "defense": self._room_for_team(match, "defense"),
        }
        if match.get("mode") == "solo":
            human_team = next((value for key, value in teams.items() if key != BOT_TEAM_KEY), "attack")
            human_room = team_rooms.get(human_team)
            human_players = [str(p).lstrip("@") for p in (human_room or {}).get("players", []) if str(p).strip()]
            bot_players = ["بوت S-Boot"]
            winner = "تعادل" if winner_team == "tie" else ("الفريق البشري" if winner_team == human_team else "بوت S-Boot")
            winning_players = human_players if winner_team == human_team else []
            team1_name, team2_name = "الفريق البشري", "S-Boot"
            team1_players, team2_players = human_players, bot_players
            team1_score = attack_score if human_team == "attack" else defense_score
            team2_score = defense_score if human_team == "attack" else attack_score
        else:
            attack_room = team_rooms.get("attack") or {}
            defense_room = team_rooms.get("defense") or {}
            team1_name = str(attack_room.get("name") or "الفريق الأول")
            team2_name = str(defense_room.get("name") or "الفريق الثاني")
            team1_players = [str(p).lstrip("@") for p in attack_room.get("players", []) if str(p).strip()]
            team2_players = [str(p).lstrip("@") for p in defense_room.get("players", []) if str(p).strip()]
            team1_score, team2_score = attack_score, defense_score
            winning_players = team1_players if winner_team == "attack" else team2_players if winner_team == "defense" else []
            winner = "تعادل" if winner_team == "tie" else team1_name if winner_team == "attack" else team2_name

        reward_lines = []
        if winning_players:
            base, remainder = divmod(prize, len(winning_players))
            points = data.setdefault("points", {})
            wins = data.setdefault("wins", {})
            for index, player in enumerate(winning_players):
                amount = base + (1 if index < remainder else 0)
                key = _user_key(player)
                points[key] = int(points.get(key, 0)) + amount
                wins[key] = int(wins.get(key, 0)) + 1
                reward_lines.append(f"💰 @{player} +{amount:,} نقطة")

        result_image = self._render_result_image(
            match=match,
            team1_name=team1_name, team1_players=team1_players,
            team1_score=team1_score, team2_name=team2_name,
            team2_players=team2_players, team2_score=team2_score,
            player_scores=player_scores, winner=winner,
            prize=prize if winning_players else 0,
        )
        images = (result_image,) if result_image else ()
        self._emit(
            data, participants,
            "🏆 انتهت مباراة الكركيت\n━━━━━━━━━━━━\n"
            f"🥇 {team1_name}: {team1_score} نقطة\n"
            f"🥈 {team2_name}: {team2_score} نقطة\n"
            f"👑 النتيجة: {winner}"
            + ("\n🎁 الجائزة 200,000 نقطة\n" + "\n".join(reward_lines) if reward_lines else ""),
            images,
        )
        data["match"] = None

    def _render_result_image(
        self,
        match: dict[str, Any],
        team1_name: str, team1_players: list[str], team1_score: int,
        team2_name: str, team2_players: list[str], team2_score: int,
        player_scores: dict[str, int], winner: str, prize: int,
    ) -> str | None:
        try:
            from cricket_result import render_result_image
            return render_result_image(
                match_id=str(match.get("id") or uuid.uuid4().hex),
                team1_name=team1_name, team1_players=team1_players, team1_score=team1_score,
                team2_name=team2_name, team2_players=team2_players, team2_score=team2_score,
                player_scores=player_scores, winner=winner, prize=prize,
                output_dir=self.state.path.parent / "cricket_media",
            )
        except Exception:
            return None

    def submit_ball(self, room: str, sender: str, number: int) -> str | None:
        room_key = _key(room)
        username = str(sender or "").strip().lstrip("@")
        user_key = _user_key(username)
        try:
            value = int(number)
        except (TypeError, ValueError):
            return "❌ اختر 0 إلى 6."
        if not 0 <= value <= 6:
            return "❌ اختر 0 إلى 6."

        def mutate(data: dict[str, Any]) -> str | None:
            match = data.get("match")
            if not isinstance(match, dict) or match.get("stage") != "live":
                return "📭 لا توجد كرة الآن."
            participants = self._participants(match)
            participant = next((item for item in participants if item["key"] == room_key), None)
            if participant is None:
                return "⛔ هذه الغرفة ليست في المباراة."
            enrolled = {_user_key(player) for player in participant.get("players", [])}
            if not user_key or user_key not in enrolled:
                return "⛔ أرسل Join أولاً."

            teams = match.get("teams") or {}
            team = teams.get(room_key)
            batting = str(match.get("batting_team") or "attack")
            bowling = "defense" if batting == "attack" else "attack"
            if team not in {batting, bowling}:
                return "⛔ فريقك غير محدد."

            batter = self._next_player(match, batting, batting=True)
            bowler = self._next_player(match, bowling, batting=False)
            choices = match.setdefault("choices", {})

            # In solo mode S-Boot chooses automatically whenever it is batting.
            if match.get("mode") == "solo" and batting == (teams.get(BOT_TEAM_KEY) or "") and not choices.get("bat"):
                choices["bat"] = {
                    "value": random.randint(0, 6),
                    "room_key": BOT_TEAM_KEY,
                    "room": "بوت S-Boot",
                    "sender": "بوت S-Boot",
                }

            if not choices.get("bat"):
                if team != batting or _user_key(batter) != user_key:
                    return f"⏳ دور المهاجم @{batter}."
                choices["bat"] = {
                    "value": value, "room_key": room_key,
                    "room": participant["name"], "sender": username,
                }
                if match.get("mode") == "solo":
                    bot_value = random.randint(0, 6)
                    bot_choice = {"value": bot_value, "room_key": BOT_TEAM_KEY, "room": "بوت S-Boot", "sender": "بوت S-Boot"}
                    bat_choice, bowl_choice = (choices["bat"], bot_choice) if batting == team else (bot_choice, choices["bat"])
                    return self._resolve_ball(data, match, participants, bat_choice, bowl_choice)

                bowl_room = self._room_for_team(match, bowling)
                if bowl_room:
                    self._emit_room_messages(
                        data,
                        {
                            room_key: f"🏏 تم تسجيل ضربة @{username}",
                            bowl_room["key"]: f"🛡️ دورك يا @{bowler}\nأرسل 0 إلى 6",
                        },
                    )
                return None

            if not choices.get("bowl"):
                if team != bowling or _user_key(bowler) != user_key:
                    return f"⏳ دور المدافع @{bowler}."
                choices["bowl"] = {
                    "value": value, "room_key": room_key,
                    "room": participant["name"], "sender": username,
                }
                return self._resolve_ball(data, match, participants, choices["bat"], choices["bowl"])

            return "⏳ انتظر الكرة التالية."

        return self.state.mutate(mutate)

    def _resolve_ball(
        self, data: dict[str, Any], match: dict[str, Any],
        participants: list[dict[str, Any]], bat_choice: dict[str, Any], bowl_choice: dict[str, Any],
    ) -> None:
        batting = str(match.get("batting_team") or "attack")
        bowling = "defense" if batting == "attack" else "attack"
        bat_value, bowl_value = int(bat_choice["value"]), int(bowl_choice["value"])
        ball_no = int(match.get("balls", 0)) + 1
        wickets = match.setdefault("wickets", {"attack": 0, "defense": 0})
        scores = match.setdefault("scores", {"attack": 0, "defense": 0})
        images = [f"cricket_ball_{bat_value}.png"]
        out_name = str(bat_choice.get("sender") or "اللاعب").strip().lstrip("@")
        bowler_name = str(bowl_choice.get("sender") or "المدافع").strip().lstrip("@")

        if bat_value == bowl_value:
            wickets[batting] = int(wickets.get(batting, 0)) + 1
            match["wicket_streak"] = int(match.get("wicket_streak", 0)) + 1
            match.setdefault("out_players", {"attack": [], "defense": []}).setdefault(batting, []).append(out_name)
            outcome = f"💥 OUT — @{out_name}"
            # First-ball dismissal = duck.
            if ball_no == 1 and int(match.get("innings", 1)) == 1:
                outcome += " 🦆 بطّة"
                images.append("cricket_duck.png")
            # Three consecutive wickets by the same bowler = hat-trick.
            if int(match["wicket_streak"]) == 3:
                outcome += f" 🔥 هاتريك — @{bowler_name}"
                images.append("cricket_hattrick.png")
        else:
            match["wicket_streak"] = 0
            scores[batting] = int(scores.get(batting, 0)) + bat_value
            player_scores = match.setdefault("player_scores", {})
            player_scores[out_name] = int(player_scores.get(out_name, 0)) + bat_value
            outcome = f"🏏 +{bat_value} نقطة"

        match["balls"] = ball_no
        match["choices"] = {}
        turns = match.setdefault("turns", {"attack": 0, "defense": 0})
        turns[batting] = int(turns.get(batting, 0)) + 1
        turns[bowling] = int(turns.get(bowling, 0)) + 1
        team_size = max(1, int(match.get("target_players") or 1))
        current_wickets = int(wickets.get(batting, 0))
        total = int(scores.get(batting, 0))

        # One compact result message, with the batter's number image delivered first.
        result_text = f"{outcome}\n📊 {total} نقطة • الكرة {ball_no}/{self.BALLS_PER_INNINGS}"
        attack_room = self._room_for_team(match, batting)
        defense_room = self._room_for_team(match, bowling)
        result_rooms = [item for item in (attack_room, defense_room) if item]

        attack_score = int(scores.get("attack", 0))
        defense_score = int(scores.get("defense", 0))
        if int(match.get("innings", 1)) == 2 and defense_score > attack_score:
            self._emit(data, result_rooms, result_text, tuple(images))
            self._finish(data, match, participants)
            return None

        innings_over = ball_no >= self.BALLS_PER_INNINGS or current_wickets >= self._team_player_count(match, batting)
        if innings_over:
            self._emit(data, result_rooms, result_text, tuple(images))
            if int(match.get("innings", 1)) == 1:
                match["innings"] = 2
                match["batting_team"] = bowling
                match["balls"] = 0
                match["wickets"][bowling] = 0
                match.setdefault("out_players", {})[bowling] = []
                match["turns"] = {"attack": 0, "defense": 0}
                match["wicket_streak"] = 0
                match["choices"] = {}
                target = attack_score + 1
                self._emit(data, participants, f"🏁 الشوط الأول انتهى\n🎯 الهدف: {target}")
                self._emit_room_messages(data, self._turn_messages(match))
            else:
                self._finish(data, match, participants)
            return None

        self._emit(data, result_rooms, result_text, tuple(images))
        self._emit_room_messages(data, self._turn_messages(match))
        return None

    def get_points(self, username: str) -> int:
        key = _user_key(username)
        data = self.state.load()
        return int((data.get("points") or {}).get(key, 0)) if key else 0

    # Public aliases used by the Talkin controller integration.
    def points_for(self, username: str) -> int:
        return self.get_points(username)

    def leaderboard(self, limit: int = 10) -> list[tuple[str, int, int]]:
        data = self.state.load()
        points = data.get("points") or {}
        wins = data.get("wins") or {}
        rows = [(str(name), int(value), int(wins.get(name, 0))) for name, value in points.items()]
        rows.sort(key=lambda item: (-item[1], -item[2], item[0]))
        return rows[:max(1, int(limit))]

    def current(self) -> dict[str, Any] | None:
        data = self.state.load()
        match = data.get("match")
        return match if isinstance(match, dict) else None

    def latest_event_id(self, room: str) -> int:
        room_key = _key(room)
        data = self.state.load()
        events = data.get("events", [])
        return max(
            (int(item.get("id", 0)) for item in events if isinstance(item, dict) and item.get("room_key") == room_key),
            default=0,
        )

    def events_after(self, room: str, event_id: int) -> list[dict[str, Any]]:
        room_key = _key(room)
        data = self.state.load()
        return [
            dict(item) for item in data.get("events", [])
            if isinstance(item, dict) and item.get("room_key") == room_key and int(item.get("id", 0)) > int(event_id)
        ]
