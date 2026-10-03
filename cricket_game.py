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
                f"👥 تم اختيار {count} لاعب(ين) في هذه الغرفة.\n"
                f"📍 الغرفة: {room_name}\n"
                "👤 كل اللاعبين يرسلون Join هنا فقط.\n"
                "🤖 عند اكتمال العدد تبدأ المباراة تلقائيًا ضد بوت S-Boot.",
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
                    return "⛔ اكتملت غرفتا المباراة. هذه الغرفة ليست مشاركة."
                # Second room joins the same global match; its players become the opposing team.
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
                            "✅ اكتمل لاعبو الغرفة.\n"
                            "🔗 المباراة الجماعية ما زالت مفتوحة؛ اجعل غرفة ثانية ترسل Join.\n"
                            "🤖 إذا أردت S-Boot بدل الغرفة الثانية، ابدأ مباراة مباشرة بـ .cr N.",
                        )
            elif len(participants) == self.ROOM_TEAMS:
                full = all(len(item.get("players", [])) >= target for item in participants)
                if full:
                    match["mode"] = "rooms"
                    match["stage"] = "teams"
                    self._emit(
                        data, participants,
                        f"🏏 اكتمل الفريقان: {target} لاعب(ين) في كل غرفة.\n"
                        "1 = هجوم | 2 = دفاع.\n"
                        "كل غرفة تختار دورها، ثم تبدأ المباراة في الغرفتين معًا.",
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
        label = self._team_label(team_value)

        def mutate(data: dict[str, Any]) -> str | None:
            match = data.get("match")
            if not isinstance(match, dict) or match.get("stage") != "teams":
                return "📭 اللعبة لا تنتظر اختيار الفرق الآن."
            participants = self._participants(match)
            participant = next((item for item in participants if item["key"] == room_key), None)
            if participant is None:
                return "⛔ هذه الغرفة ليست مشاركة في المباراة. أرسل Join أولاً."
            teams = match.setdefault("teams", {})
            teams[room_key] = team_value
            if match.get("mode") == "solo":
                teams[BOT_TEAM_KEY] = "defense" if team_value == "attack" else "attack"
                self._start_live(data, match, participants)
                return None

            both_sides = "attack" in teams.values() and "defense" in teams.values()
            everyone_chose = all(item["key"] in teams for item in participants)
            if both_sides and everyone_chose:
                self._start_live(data, match, participants)
            else:
                if both_sides:
                    text = f"✅ اختارت غرفة {participant['name']} فريق {label}. بانتظار اختيار الغرفة الأخرى."
                else:
                    text = f"✅ اختارت غرفة {participant['name']} فريق {label}. يجب أن تختار الغرفة الأخرى الدور المقابل."
                self._emit(data, participants, text)
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
        batting = str(match.get("batting_team") or "attack")
        bowling = "defense" if batting == "attack" else "attack"
        if match.get("mode") == "solo":
            human_team = next(
                (value for key, value in (match.get("teams") or {}).items() if key != BOT_TEAM_KEY),
                "attack",
            )
            if batting == human_team:
                batter = self._next_player(match, human_team, batting=True)
                return f"🎯 دور الضارب @{batter}: أرسل رقمًا من 0 إلى 6، وسيختار بوت S-Boot تخمينه."
            bowler = self._next_player(match, human_team, batting=False)
            return f"🛡️ دور @{bowler} للدفاع: أرسل رقمًا من 0 إلى 6 لتخمين ضربة بوت S-Boot."
        batter = self._next_player(match, batting, batting=True)
        bowler = self._next_player(match, bowling, batting=False)
        return (
            f"🎯 دور الضارب @{batter} من فريق {self._team_label(batting)}، "
            f"ودور المخمّن @{bowler} من فريق {self._team_label(bowling)}.\n"
            "يرسل كل منهما رقمًا من 0 إلى 6."
        )

    def _start_live(self, data: dict[str, Any], match: dict[str, Any], participants: list[dict[str, Any]]) -> None:
        match["stage"] = "live"
        match["innings"] = 1
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
        self._emit(
            data,
            participants,
            "🏏 بدأت المباراة! فريق الهجوم يضرب أولاً.\n" + self._turn_prompt(match),
        )

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

    @staticmethod
    def _render_result_image(
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
            return "❌ اختر رقمًا من 0 إلى 6."
        if not 0 <= value <= 6:
            return "❌ اختر رقمًا من 0 إلى 6."

        def mutate(data: dict[str, Any]) -> str | None:
            match = data.get("match")
            if not isinstance(match, dict) or match.get("stage") != "live":
                return "📭 لا توجد كرة تنتظر الاختيارات الآن."
            participants = self._participants(match)
            participant = next((item for item in participants if item["key"] == room_key), None)
            if participant is None:
                return "⛔ هذه الغرفة ليست مشاركة في المباراة."
            enrolled = {_user_key(player) for player in participant.get("players", [])}
            if not user_key or user_key not in enrolled:
                return "⛔ يجب أن تسجل أولاً بإرسال Join قبل اللعب."

            teams = match.get("teams") or {}
            team = teams.get(room_key)
            batting = str(match.get("batting_team") or "attack")
            bowling = "defense" if batting == "attack" else "attack"

            if match.get("mode") == "solo":
                human_key = next((key for key in teams if key != BOT_TEAM_KEY), room_key)
                human_team = str(teams.get(human_key) or "attack")
                if batting == human_team:
                    expected = self._next_player(match, human_team, batting=True)
                    side = "bat"
                else:
                    expected = self._next_player(match, human_team, batting=False)
                    side = "bowl"
                if _user_key(expected) != user_key:
                    role = "الضارب" if side == "bat" else "المخمّن"
                    return f"⏳ الدور الآن على @{expected} ({role})."
                bot_value = random.randint(0, 6)
                human_choice = {"value": value, "room_key": room_key, "room": participant["name"], "sender": username}
                bot_choice = {"value": bot_value, "room_key": BOT_TEAM_KEY, "room": "بوت S-Boot", "sender": "بوت S-Boot"}
                bat_choice, bowl_choice = (human_choice, bot_choice) if batting == human_team else (bot_choice, human_choice)
                return self._resolve_ball(data, match, participants, bat_choice, bowl_choice)

            # Determine the role of this room for the current ball.
            # Without this assignment, `side` was undefined in two-room
            # matches and numeric choices failed with NameError.
            if team == batting:
                expected = self._next_player(match, batting, batting=True)
                side = "bat"
            elif team == bowling:
                expected = self._next_player(match, bowling, batting=False)
                side = "bowl"
            else:
                return "⛔ لم يتم تحديد فريق غرفتك في المباراة."
            if _user_key(expected) != user_key:
                role = "الضارب" if side == "bat" else "المخمّن"
                return f"⏳ الدور الآن على @{expected} ({role})."

            choices = match.setdefault("choices", {})
            if choices.get(side):
                return "⏳ سجّل لاعب فريقك اختياره لهذه الكرة بالفعل."
            choices[side] = {
                "value": value,
                "room_key": room_key,
                "room": participant["name"],
                "sender": username,
            }
            if not (choices.get("bat") and choices.get("bowl")):
                side_label = "الهجوم" if side == "bat" else "الدفاع"
                self._emit(data, [participant], f"✅ سجّل @{username} الرقم {value} لفريق {side_label}. بانتظار الفريق الآخر.")
                return None
            return self._resolve_ball(data, match, participants, choices["bat"], choices["bowl"])

        return self.state.mutate(mutate)

    def _resolve_ball(
        self,
        data: dict[str, Any],
        match: dict[str, Any],
        participants: list[dict[str, Any]],
        bat_choice: dict[str, Any],
        bowl_choice: dict[str, Any],
    ) -> None:
        batting = str(match.get("batting_team") or "attack")
        bowling = "defense" if batting == "attack" else "attack"
        bat_value, bowl_value = int(bat_choice["value"]), int(bowl_choice["value"])
        ball_no = int(match.get("balls", 0)) + 1
        wickets = match.setdefault("wickets", {"attack": 0, "defense": 0})
        scores = match.setdefault("scores", {"attack": 0, "defense": 0})
        images = [f"cricket_ball_{bat_value}.png"]
        if bat_value == bowl_value:
            wickets[batting] = int(wickets.get(batting, 0)) + 1
            match["wicket_streak"] = int(match.get("wicket_streak", 0)) + 1
            out_name = str(bat_choice.get("sender") or "اللاعب")
            match.setdefault("out_players", {"attack": [], "defense": []}).setdefault(batting, []).append(out_name)
            outcome = f"ويكيت! خرج @{out_name}."
            if int(match.get("innings", 1)) == 1 and ball_no == 1 and int(wickets[batting]) == 1:
                outcome += " 🦆 خرج من أول كرة — دَك!"
                images.append("cricket_duck.png")
            if int(match["wicket_streak"]) == 3:
                outcome += " 🔥 هاتريك — ثلاث ويكيت متتالية!"
                images.append("cricket_hattrick.png")
        else:
            match["wicket_streak"] = 0
            scores[batting] = int(scores.get(batting, 0)) + bat_value
            scorer = str(bat_choice.get("sender") or bat_choice.get("room") or "اللاعب").strip().lstrip("@")
            player_scores = match.setdefault("player_scores", {})
            player_scores[scorer] = int(player_scores.get(scorer, 0)) + bat_value
            outcome = f"سجّل @{scorer} {bat_value} نقطة." if bat_value else "كرة بلا نقاط؛ لا يوجد ويكيت."

        match["balls"] = ball_no
        match["choices"] = {}
        turns = match.setdefault("turns", {"attack": 0, "defense": 0})
        turns[batting] = int(turns.get(batting, 0)) + 1
        turns[bowling] = int(turns.get(bowling, 0)) + 1
        team_size = max(1, int(match.get("target_players") or 1))
        current_wickets = int(wickets.get(batting, 0))
        text = (
            f"🏏 الشوط {match.get('innings', 1)} — الكرة {ball_no}/{self.BALLS_PER_INNINGS}\n"
            f"⚔️ الضارب @{bat_choice.get('sender') or bat_choice['room']} اختار {bat_value} | "
            f"🛡️ المخمّن @{bowl_choice.get('sender') or bowl_choice['room']} اختار {bowl_value}\n"
            f"{outcome}\n📊 النتيجة — الهجوم: {scores.get('attack', 0)} | الدفاع: {scores.get('defense', 0)}"
            f"\n🚫 ويكيت هذا الشوط: {current_wickets}/{team_size}"
        )
        attack_score = int(scores.get("attack", 0))
        defense_score = int(scores.get("defense", 0))
        if int(match.get("innings", 1)) == 2 and defense_score > attack_score:
            self._emit(data, participants, text, tuple(images))
            self._finish(data, match, participants)
            return None

        innings_over = ball_no >= self.BALLS_PER_INNINGS or current_wickets >= self._team_player_count(match, batting)
        if innings_over:
            self._emit(data, participants, text, tuple(images))
            if int(match.get("innings", 1)) == 1:
                match["innings"] = 2
                match["batting_team"] = "defense"
                match["balls"] = 0
                match["wickets"]["defense"] = 0
                match.setdefault("out_players", {})["defense"] = []
                match["turns"] = {"attack": 0, "defense": 0}
                match["wicket_streak"] = 0
                match["choices"] = {}
                target = attack_score + 1
                self._emit(
                    data,
                    participants,
                    f"🏁 انتهى الشوط الأول. هدف فريق الدفاع: {target} نقطة.\n"
                    "تبدّل الهجوم والدفاع.\n" + self._turn_prompt(match),
                )
            else:
                self._finish(data, match, participants)
            return None

        self._emit(data, participants, text + "\n" + self._turn_prompt(match), tuple(images))
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
