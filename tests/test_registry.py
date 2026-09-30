import tempfile
import subprocess
import sys
import unittest
from pathlib import Path

from control_server import BotServerService
from control_runtime import ControlAccountBot
from friendships import accept_friend_query, request_friendships_query, request_usernames
from registry import BotRegistry, BotSpec, RegistryError
from state import CredentialVault, StateError, load_or_create_state_key, status_value
from vendor.talkin_runtime import decode_message


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

    def test_server_status_uses_blue_in_place_of_yellow(self):
        source = Path(__file__).resolve().parents[1] / "app.py"
        app_text = source.read_text(encoding="utf-8")
        self.assertIn("#60A5FA", app_text)
        self.assertNotIn("#FFD166'>Bot Entry Server", app_text)

    def test_invitation_flow_avoids_the_stale_owner_list_gate(self):
        source = Path(__file__).resolve().parents[1] / "restricted_runtime.py"
        runtime_text = source.read_text(encoding="utf-8")
        start = runtime_text.index("    def _start_invites")
        end = runtime_text.index("    def _handle_invitation_roster", start)
        invite_method = runtime_text[start:end]
        self.assertIn("_settings_room_users", invite_method)
        self.assertIn("occupants_list", invite_method)
        self.assertNotIn("_bot_is_room_owner", invite_method)

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
