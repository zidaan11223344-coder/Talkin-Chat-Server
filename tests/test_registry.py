import tempfile
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from control_server import BotServerService
from control_runtime import ControlAccountBot
from cricket_game import CricketGame
from friendships import accept_friend_query, request_friendships_query, request_usernames
from invite_templates import InviteTemplateStore
from registry import BotRegistry, BotSpec, RegistryError
from room_actions import RoomActionQueue
from restricted_runtime import RestrictedTalkinBot
from state import CredentialVault, StateError, load_or_create_state_key, status_value
from vendor.talkin_runtime import TalkinBot, decode_message


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = CredentialVault.generate()
        self.registry = BotRegistry(self.temp.name, self.key, "Admin")

    def tearDown(self):
        self.temp.cleanup()

    def test_password_is_encrypted_and_controller_status_is_coloured(self):
        record = self.registry.create(BotSpec("control", "secret-pass", "Main Room", "controller", "master"))
        raw = Path(self.temp.name, "talkin_bot_server.json").read_text(encoding="utf-8")
        self.assertNotIn("secret-pass", raw)
        loaded = self.registry.get(record["id"], include_password=True)
        self.assertEqual(loaded["password"], "secret-pass")
        self.assertIn("بوت متحكم", loaded["profile_status"])
        self.assertIn("#FFD166", loaded["profile_status"])

    def test_room_has_only_one_controller_and_allows_silent_bot(self):
        self.registry.create(BotSpec("control", "pass", "Room", "controller", "master"))
        with self.assertRaises(RegistryError):
            self.registry.create(BotSpec("control2", "pass", "Room", "controller", "master"))
        silent = self.registry.create(BotSpec("quiet", "pass", "Room", "silent", "master"))
        self.assertEqual(silent["role"], "silent")
        self.assertIn("بوت صامت", silent["profile_status"])

    def test_controller_bots_can_be_registered_for_different_rooms(self):
        first = self.registry.create(BotSpec("control_a", "pass-a", "Room A", "controller", "master"))
        second = self.registry.create(BotSpec("control_b", "pass-b", "Room B", "controller", "master"))
        self.assertEqual({first["room"], second["room"]}, {"Room A", "Room B"})

    def test_delegate_can_add_silent_bot_but_not_controller(self):
        self.registry.create(BotSpec("control", "pass", "Room", "controller", "master"))
        self.registry.add_delegate("Room", "master", "helper")
        bot = self.registry.create(BotSpec("quiet", "pass", "Room", "silent", "helper"))
        self.assertEqual(bot["master"], "master")
        with self.assertRaises(RegistryError):
            self.registry.create(BotSpec("other", "pass", "Room", "controller", "helper"))

    def test_service_parses_passwords_with_at_signs(self):
        service = BotServerService(self.temp.name, self.key, "Admin", spawn=False)
        reply = service.handle_command("master", "control@p@ss@Room One")
        self.assertIn("تمت إضافة", reply)
        bot = service.registry.visible_bots("master")[0]
        full = service.registry.get(bot["id"], include_password=True)
        self.assertEqual(full["password"], "p@ss")

    def test_spawned_child_receives_the_persistent_encryption_key(self):
        service = BotServerService(self.temp.name, self.key, "Admin")
        record = service.registry.create(BotSpec("child", "child-secret", "Room", "controller", "master"))
        fake_process = type("FakeProcess", (), {"pid": 12345, "poll": lambda self: None})()
        with patch("control_server.subprocess.Popen", return_value=fake_process) as popen, \
             patch("control_server.threading.Thread"):
            service.start_record(record)
        child_env = popen.call_args.kwargs["env"]
        self.assertEqual(child_env["STATE_ENCRYPTION_KEY"], self.key)
        self.assertEqual(child_env["BOT_SERVER_DATA_DIR"], str(service.data_dir))

    def test_restricted_runtime_handles_talkin_top_level_room_join_results(self):
        def process_response(payload, pending):
            bot = RestrictedTalkinBot.__new__(RestrictedTalkinBot)
            bot._pending_room_joins = pending
            bot._pending_room_lists = {}
            events = []
            bot.handle_room_event = lambda result: events.append(result["room_event"])
            with patch("restricted_runtime.decode_result_message", return_value=payload):
                bot._process_message(None, b"test")
            return events

        joined = process_response(
            {"type": "success", "value": "room a"},
            {"Room A": {"room": "Room A"}},
        )
        self.assertEqual(joined, [{1: "you_joined", 13: "Room A"}])

        failed = process_response(
            {"type": "room_full", "value": ""},
            {"Room A": {"room": "Room A"}},
        )
        self.assertEqual(failed, [{1: "room_full", 13: "Room A"}])

        unrelated = process_response({"type": "success", "value": "Room A"}, {})
        self.assertEqual(unrelated, [])

    def test_room_join_timeout_marks_bot_offline_and_notifies_master(self):
        record = self.registry.create(BotSpec("control", "pass", "Room A", "controller", "master"))
        bot = RestrictedTalkinBot.__new__(RestrictedTalkinBot)
        bot.record_id = record["id"]
        bot.registry_root = Path(self.temp.name)
        bot.master = "master"
        bot._pending_room_joins = {"Room A": {"room": "Room A"}}
        messages = []
        bot.send_private_text = lambda user, text: messages.append((user, text))
        bot.log = lambda *_args: None

        with patch("restricted_runtime.TalkinBot._join_timeout", autospec=True) as base_timeout, \
             patch.dict("os.environ", {
                 "STATE_ENCRYPTION_KEY": self.key,
                 "SERVER_ADMIN_NAME": "Admin",
                 "BOT_JOIN_NOTIFY_MASTER": "1",
             }), \
             patch("builtins.print"):
            bot._join_timeout("Room A")

        base_timeout.assert_called_once_with(bot, "Room A")
        updated = self.registry.get(record["id"])
        self.assertEqual(updated["status"], "offline")
        self.assertIn("Timed out", updated["error"])
        self.assertEqual(messages[0][0], "master")
        self.assertIn("Room A", messages[0][1])

    def test_server_status_uses_blue_in_place_of_yellow(self):
        source = Path(__file__).resolve().parents[1] / "app.py"
        app_text = source.read_text(encoding="utf-8")
        self.assertIn("#60A5FA", app_text)
        self.assertNotIn("#FFD166'>Bot Entry Server", app_text)

    def test_invitation_flow_finishes_all_room_lists_before_sending(self):
        source = Path(__file__).resolve().parents[1] / "restricted_runtime.py"
        runtime_text = source.read_text(encoding="utf-8")
        start = runtime_text.index("    def _start_invites")
        end = runtime_text.index("    def _handle_invitation_roster", start)
        invite_method = runtime_text[start:end]
        self.assertIn("request_occupants", invite_method)
        self.assertIn("response_to=requester", invite_method)
        self.assertNotIn("_bot_is_room_owner", invite_method)
        process_start = runtime_text.index("    def _process_message", end)
        process_end = runtime_text.index("    def start", process_start)
        process_method = runtime_text[process_start:process_end]
        self.assertLess(process_method.index("_complete_pending_room_list(result)"),
                        process_method.index("process_occupants_for_invite(result)"))

    def test_help_contains_requested_paged_moderation_commands(self):
        source = Path(__file__).resolve().parents[1] / "restricted_runtime.py"
        runtime_text = source.read_text(encoding="utf-8")
        for text in ("📋 أوامر الإدارة — 1 | الإدارة والحظر", "ub@اسم", "m@اسم", "📌 للقائمة التالية اكتب ns"):
            self.assertIn(text, runtime_text)
        self.assertNotIn("bl@اسم", runtime_text)
        self.assertNotIn('re.fullmatch(r"bl@', runtime_text)
        self.assertIn("def _advance_restricted_list_page", runtime_text)

    def test_restricted_admin_aliases_and_nonmaster_help_dispatch(self):
        bot = RestrictedTalkinBot.__new__(RestrictedTalkinBot)
        bot.role = "controller"
        bot.target_room = "Room A"
        bot._is_own_room = lambda room: room == "Room A"
        bot._is_master = lambda _sender: True
        bot._cricket = type("Game", (), {"current": lambda self: None})()
        bot._help_page_by_sender = {}
        bot._command_is_private = False
        bot._handle_protection_command = lambda *_args: False
        calls = []
        bot._moderate = lambda room, target, action: calls.append((target, action))
        bot._undo_last_bot_action = lambda sender: calls.append((sender, "undo"))
        for command in ("m@Alice", "ub@Bob", "kick Carol", "unban Dana", ".u"):
            self.assertTrue(bot._handle_controller_command("Room A", "master", command))
        self.assertEqual(calls, [
            ("Alice", "grant_member"), ("Bob", "member"), ("Carol", "kick"),
            ("Dana", "member"), ("master", "undo"),
        ])
        bot._is_master = lambda _sender: False
        help_calls = []
        bot._send_help = lambda room, private_to="", page=1: help_calls.append((room, private_to, page))
        self.assertTrue(bot._handle_controller_command("Room A", "guest", "help"))
        self.assertEqual(help_calls, [("Room A", "", 1)])

    def test_ns_advances_private_result_page_using_room_and_sender_key(self):
        bot = RestrictedTalkinBot.__new__(RestrictedTalkinBot)
        bot._result_pages = {
            ("chat_message", "Room A", "master"): {"pages": ["first", "second"], "part": 1}
        }
        bot._command_is_private = True
        sent = []
        bot.send_private_text = lambda sender, text: sent.append((sender, text))
        self.assertTrue(bot._advance_restricted_list_page("Room A", "master"))
        self.assertEqual(sent, [("master", "second\n\n✅ انتهت القوائم.")])

    def test_private_invite_history_is_one_bounded_message(self):
        bot = RestrictedTalkinBot.__new__(RestrictedTalkinBot)
        bot.sent_invites = lambda _room: [f"user{i}" for i in range(200)]
        sent = []
        bot.send_private_text = lambda username, text: sent.append((username, text))
        bot.send_room_text = lambda *_args: self.fail("private history must not post to the room")
        with patch.dict("os.environ", {"WS_MAX_MESSAGE_BYTES": "1008"}):
            bot._send_invite_history("Room A", private_to="master")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "master")
        self.assertIn("الإجمالي 200", sent[0][1])

    def test_invitation_template_keeps_a_room_placeholder_and_persists(self):
        store = InviteTemplateStore(self.temp.name)
        saved = store.set("Room A", "أهلًا، انضم إلينا")
        self.assertEqual(saved, "أهلًا، انضم إلينا ({room})")
        self.assertEqual(store.render("Room A", "friend", "master"), "أهلًا، انضم إلينا (Room A)")
        self.assertEqual(store.get("room a"), saved)
        store.reset("ROOM A")
        self.assertEqual(store.get("Room A"), "يوجد معجب مخفي في ({room})")
        self.assertEqual(store.ensure_room("Welcome {room}"), "Welcome ({room})")

    def test_all_room_ban_queue_is_persistent_and_deduplicates_rooms(self):
        queue = RoomActionQueue(self.temp.name)
        action_id, count = queue.enqueue("bad_user", ["Room A", "room a", "Room B"], "master")
        self.assertEqual(count, 2)
        self.assertEqual(len(queue.pending_for_room("ROOM A")), 1)
        queue.mark_done(action_id, "Room A")
        self.assertEqual(queue.pending_for_room("room a"), [])
        self.assertEqual(len(queue.pending_for_room("Room B")), 1)

    def test_cricket_two_players_in_one_room_and_turn_order(self):
        game = CricketGame(self.temp.name)
        game.set_enabled("Room A", True)
        self.assertIsNone(game.start("Room A", 2))
        self.assertIsNone(game.join("Room A", "alpha"))
        self.assertIsNone(game.join("Room A", "beta"))
        state = game.current()
        self.assertEqual(state["stage"], "live")
        self.assertEqual(state["target_players"], 2)
        self.assertEqual(len(state["rooms"]), 1)
        self.assertEqual(state["rooms"][0]["players"], ["alpha", "beta"])
        with patch("cricket_game.random.randint", return_value=6):
            self.assertIsNone(game.submit_ball("Room A", "alpha", 3))
        self.assertIn("alpha", "\n".join(event["text"] for event in game.events_after("Room A", 0)))

    def test_cricket_accepts_one_to_four_players_in_one_room(self):
        for count in range(1, 5):
            with self.subTest(players=count):
                game = CricketGame(Path(self.temp.name) / f"size-{count}")
                game.set_enabled("Room A", True)
                self.assertIsNone(game.start("Room A", count))
                for index in range(count):
                    self.assertIsNone(game.join("Room A", f"p{index}"))
                match = game.current()
                self.assertEqual(match["target_players"], count)
                self.assertEqual(match["stage"], "live")
                self.assertEqual(len(match["rooms"]), 1)
                self.assertEqual(len(match["rooms"][0]["players"]), count)
                self.assertEqual(match["mode"], "solo")

    def test_single_room_turns_images_and_prize_points(self):
        game = CricketGame(self.temp.name)
        game.set_enabled("Room A", True)
        self.assertIsNone(game.start("Room A", 2))
        self.assertIsNone(game.join("Room A", "alpha"))
        self.assertIsNone(game.join("Room A", "beta"))
        # Six human batting balls, then six bot batting balls. Human players rotate.
        with patch("cricket_game.random.randint", side_effect=[6, 6, 6, 6, 6, 6, 0, 0, 0, 0, 0, 0]):
            for user in ("alpha", "beta", "alpha", "beta", "alpha", "beta"):
                self.assertIsNone(game.submit_ball("Room A", user, 1))
            self.assertEqual(game.current()["innings"], 2)
            for user in ("alpha", "beta", "alpha", "beta", "alpha", "beta"):
                self.assertIsNone(game.submit_ball("Room A", user, 1))
        self.assertIsNone(game.current())
        events = game.events_after("Room A", 0)
        images = [image for event in events for image in event["images"]]
        self.assertIn("cricket_number_1.png", images)
        self.assertIn("cricket_result_", "\n".join(images))
        self.assertIn("بوت S-Boot", "\n".join(event["text"] for event in events))
        self.assertEqual(game.get_points("alpha"), 100000)
        self.assertEqual(game.get_points("beta"), 100000)

    def test_single_player_can_play_against_the_controller_bot(self):
        game = CricketGame(self.temp.name)
        game.set_enabled("Room A", True)
        self.assertIsNone(game.start("Room A", 1))
        self.assertIsNone(game.join("Room A", "solo_player"))
        self.assertEqual(game.current()["stage"], "live")
        with patch("cricket_game.random.randint", side_effect=[2, 2, 2, 2, 2, 2, 0, 0, 0, 0, 0, 0]):
            for _ in range(6):
                self.assertIsNone(game.submit_ball("Room A", "solo_player", 1))
            for _ in range(6):
                self.assertIsNone(game.submit_ball("Room A", "solo_player", 1))
        self.assertIsNone(game.current())
        images = [image for event in game.events_after("Room A", 0) for image in event["images"]]
        self.assertIn("cricket_number_1.png", images)
        delivered_media = []
        controller = RestrictedTalkinBot.__new__(RestrictedTalkinBot)
        controller.role = "controller"
        controller.target_room = "Room A"
        controller._cricket = game
        controller._cricket_cursor = 0
        controller._cricket_delivery_lock = threading.Lock()
        controller._cricket_asset_url = lambda filename: f"https://assets.test/{filename}"
        controller.send_room_media = lambda room, url, kind: delivered_media.append((room, url, kind))
        controller.send_room_text = lambda *_args: None
        controller._deliver_cricket_events()
        self.assertIn(("Room A", "https://assets.test/cricket_number_1.png", "image"), delivered_media)

    def test_controller_commands_route_size_selection_and_solo_bot(self):
        controller = RestrictedTalkinBot.__new__(RestrictedTalkinBot)
        controller.role = "controller"
        controller.target_room = "Room A"
        controller._is_own_room = lambda room: room == "Room A"
        controller._is_master = lambda sender: sender == "master"
        controller._cricket = CricketGame(self.temp.name)
        controller._deliver_cricket_events = lambda: None
        controller.send_room_text = lambda *_args: None
        controller._cricket.set_enabled("Room A", True)
        self.assertTrue(controller._handle_controller_command("Room A", "master", ".cricket 3"))
        self.assertEqual(controller._cricket.current()["stage"], "lobby")
        self.assertEqual(controller._cricket.current()["target_players"], 3)
        self.assertTrue(controller._handle_controller_command("Room A", "player1", "Join"))
        self.assertTrue(controller._handle_controller_command("Room A", "player2", "Join"))
        self.assertTrue(controller._handle_controller_command("Room A", "player3", "Join"))
        self.assertEqual(controller._cricket.current()["stage"], "live")
        self.assertEqual(len(controller._cricket.current()["rooms"][0]["players"]), 3)

    def test_all_cricket_images_are_transparent_and_under_100_kb(self):
        root = Path(__file__).resolve().parents[1] / "vendor" / "assets"
        expected = [*(f"cricket_number_{i}.png" for i in range(0, 7)), "cricket_duck.png", "cricket_hattrick.png"]
        for name in expected:
            path = root / name
            self.assertTrue(path.is_file(), name)
            self.assertLess(path.stat().st_size, 100_000, name)
            header = path.read_bytes()[:26]
            self.assertEqual(header[25], 6, f"{name} must be a true RGBA PNG")

    @staticmethod
    def _query_fields(packet):
        return {key: values[0] for key, values in decode_message(packet).items()}

    def test_friend_request_packets_match_talkin_android_protocol(self):
        poll = self._query_fields(request_friendships_query())
        self.assertEqual(poll[1], b"profile_update")
        self.assertEqual(poll[2], b"send_requests")
        self.assertEqual(poll[11], b"")
        accept = self._query_fields(accept_friend_query("@newfriend"))
        self.assertEqual(accept[1], b"profile_update")
        self.assertEqual(accept[2], b"accept_friend")
        self.assertEqual(accept[11], b"newfriend")

    def test_friend_request_response_handler_extracts_unique_usernames(self):
        result = {"handler_id": 17, "users": [{1: "Alice"}, {1: "@alice"}, {1: "Bob"}]}
        self.assertEqual(request_usernames(result), ["Alice", "Bob"])
        self.assertEqual(request_usernames({"handler_id": 18, "users": [{1: "NotRequest"}]}), [])

    def test_control_bot_accepts_and_welcomes_without_database_access(self):
        class FakeRegistry:
            def __init__(self):
                self.pending = []
            def set_pending_language(self, username):
                self.pending.append(username)

        class FakeService:
            def __init__(self):
                self.registry = FakeRegistry()

        bot = ControlAccountBot.__new__(ControlAccountBot)
        bot.service = FakeService()
        bot._accepted_friends = set()
        sent_queries, welcomes = [], []
        bot.send_query = sent_queries.append
        bot.send_private_text = lambda username, text: welcomes.append((username, text)) or True
        count = bot._accept_friend_requests({"handler_id": 17, "users": [{1: "newfriend"}]})
        self.assertEqual(count, 1)
        self.assertEqual(bot.service.registry.pending, ["newfriend"])
        self.assertIn("لاختيار العربية أرسل 1", welcomes[0][1])
        self.assertEqual(self._query_fields(sent_queries[0])[2], b"accept_friend")

    def test_private_help_is_localized_and_explains_add_commands(self):
        service = BotServerService(self.temp.name, self.key, "Admin", spawn=False)
        service.registry.set_pending_language("arabic_user")
        service.registry.choose_language("arabic_user", "1")
        arabic = service.handle_command("arabic_user", "help")
        self.assertIn("اسم_البوت@كلمة_مروره@اسم_الغرفة", arabic)
        service.registry.set_pending_language("english_user")
        service.registry.choose_language("english_user", "2")
        english = service.handle_command("english_user", "help")
        self.assertIn("bot_username@bot_password@room_name", english)

    def test_key_generator_outputs_a_valid_fernet_key(self):
        script = Path(__file__).resolve().parents[1] / "generate_key.py"
        result = subprocess.run([sys.executable, str(script)], check=True, capture_output=True, text=True)
        CredentialVault(result.stdout.strip())

    def test_first_start_creates_key_once_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            first_key = load_or_create_state_key(data_dir)
            key_file = data_dir / ".state_encryption_key"
            self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
            registry = BotRegistry(data_dir, first_key, "Admin")
            record = registry.create(BotSpec("control", "secret", "Room", "controller", "master"))
            second_key = load_or_create_state_key(data_dir)
            self.assertEqual(second_key, first_key)
            self.assertEqual(BotRegistry(data_dir, second_key, "Admin").get(record["id"], True)["password"], "secret")

    def test_first_start_refuses_to_replace_a_missing_key_for_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "talkin_bot_server.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(StateError):
                load_or_create_state_key(directory)

    def test_friend_acceptance_has_no_supabase_dependency_or_environment(self):
        root = Path(__file__).resolve().parents[1]
        self.assertNotIn("supabase", (root / "requirements.txt").read_text(encoding="utf-8").casefold())
        self.assertNotIn("SUPABASE_", (root / ".env.example").read_text(encoding="utf-8"))
        source = (root / "friendships.py").read_text(encoding="utf-8")
        self.assertIn('encode_query("profile_update", type_="send_requests"', source)
        self.assertIn('type_="accept_friend"', source)


if __name__ == "__main__":
    unittest.main()
