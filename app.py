"""Runs the S-Boot Talkin bot-entry service and its health endpoint."""
from __future__ import annotations

import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

from control_server import BotServerService
from state import CredentialVault, StateError, load_or_create_state_key, safe_state_root


class HealthHandler(BaseHTTPRequestHandler):
    service: BotServerService | None = None

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") not in {"", "/health", "/status"}:
            self.send_response(404); self.end_headers(); return
        payload = json.dumps((self.service.health() if self.service else {"status": "starting"}), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


def main() -> int:
    load_dotenv(override=False)
    server_id = os.getenv("SERVER_BOT_ID", "").strip().lstrip("@")
    server_password = os.getenv("SERVER_BOT_PASSWORD", "")
    data_dir = safe_state_root(os.getenv("BOT_SERVER_DATA_DIR", "./data"))
    state_key = os.getenv("STATE_ENCRYPTION_KEY", "").strip()
    admin_name = os.getenv("SERVER_ADMIN_NAME", "ۦاݪــۛـسـ𓆩♛𓆪ـۧۦـ۫ـفـيــ۫ـۧ𝁤𝆬𝃛")
    if not server_id or not server_password:
        raise SystemExit("Missing SERVER_BOT_ID or SERVER_BOT_PASSWORD")
    if not state_key:
        key_existed = (data_dir / ".state_encryption_key").exists()
        state_key = load_or_create_state_key(data_dir)
        print("Loaded persistent encryption key." if key_existed else "Created persistent encryption key on first startup.", flush=True)
    else:
        CredentialVault(state_key)
    print(f"S-Boot runtime data directory: {data_dir}", flush=True)

    # The central account is a private command server and must not join a room.
    # These values are read at Talkin runtime import time.
    os.environ.update({
        "BOT_ID": server_id,
        "BOT_PWD": server_password,
        "BOT_MASTER": server_id,
        "GROUP_TO_JOIN": "",
        "RUNNING_AS_MASTER": "1",
        "MASTER_SERVICE_ENABLED": "1",
        "ASSET_HTTP_ENABLED": "0",
        "GITHUB_SYNC": "0",
        "BOT_DATA_DIR": str(data_dir / "control"),
        "BOT_FIRST_CONNECTION_STATUS": os.getenv("SERVER_PROFILE_STATUS", "<B><H4><div style='background-color:#101827;padding:9px;text-align:center;'><font color='#FF5A5F'>🛡️ -sbot-</font><br><font color='#C4B5FD'>سيرفر إدخال بوتات تحكم وصامتة</font><br><font color='#34D399'>الإدارة • الحماية • الدعوات • القوائم • الكركيت</font><br><font color='#60A5FA'>Bot Entry Server • Admin • Protection • Invites • Lists • Cricket</font></div></H4></B>"),
        "BOT_BASE_STATUS": os.getenv("SERVER_PROFILE_STATUS", "<B><H4><div style='background-color:#101827;padding:9px;text-align:center;'><font color='#FF5A5F'>🛡️ -sbot-</font><br><font color='#C4B5FD'>سيرفر إدخال بوتات تحكم وصامتة</font><br><font color='#34D399'>الإدارة • الحماية • الدعوات • القوائم • الكركيت</font><br><font color='#60A5FA'>Bot Entry Server • Admin • Protection • Invites • Lists • Cricket</font></div></H4></B>"),
    })

    # Import after the environment above has been installed.
    from control_runtime import ControlAccountBot

    service = BotServerService(data_dir, state_key, admin_name)
    control = ControlAccountBot(service)
    service.restore()
    control_thread = threading.Thread(target=control.start, name="sboot-control-account", daemon=True)
    control_thread.start()

    stop = threading.Event()
    def friendship_loop():
        # Poll Talkin's native profile_update/send_requests command; no
        # Supabase credentials or friendship-table access are required.
        while not stop.wait(max(2.0, float(os.getenv("FRIEND_POLL_SECONDS", "10")))):
            if getattr(control, "ws", None):
                control.poll_friend_requests()
    friend_thread = threading.Thread(target=friendship_loop, name="sboot-friends", daemon=True)
    friend_thread.start()

    HealthHandler.service = service
    port = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)

    def shutdown(_signum=None, _frame=None):
        stop.set(); service.shutdown()
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"S-Boot server health endpoint listening on 0.0.0.0:{port}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StateError as exc:
        raise SystemExit(str(exc)) from exc
