import tempfile
import unittest
from pathlib import Path

from control_server import BotServerService
from registry import BotRegistry, BotSpec, RegistryError
from state import CredentialVault, status_value


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


if __name__ == "__main__":
    unittest.main()
