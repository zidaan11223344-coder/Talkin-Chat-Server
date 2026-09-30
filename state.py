"""Atomic, encrypted runtime state for the Talkin S-Boot server."""
from __future__ import annotations

import base64
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, TypeVar

from cryptography.fernet import Fernet, InvalidToken

T = TypeVar("T")


class StateError(RuntimeError):
    """Raised for invalid or unreadable server state."""


class CredentialVault:
    """Encrypts bot passwords before they are persisted on the mounted volume."""

    def __init__(self, key: str):
        key = str(key or "").strip()
        if not key:
            raise StateError("STATE_ENCRYPTION_KEY is required to store bot passwords safely")
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except Exception as exc:
            raise StateError("STATE_ENCRYPTION_KEY must be a Fernet key") from exc

    @staticmethod
    def generate() -> str:
        return Fernet.generate_key().decode("ascii")

    def seal(self, value: str) -> str:
        return self._fernet.encrypt(str(value).encode("utf-8")).decode("ascii")

    def open(self, token: str) -> str:
        try:
            return self._fernet.decrypt(str(token).encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise StateError("stored bot credential cannot be decrypted") from exc


class JsonState:
    """File-locked JSON state with atomic replacements and 0600 permissions."""

    def __init__(self, path: str | Path, default: Callable[[], Any]):
        self.path = Path(path)
        self.default = default
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _lock(self):
        self.lock_path.touch(mode=0o600, exist_ok=True)
        with self.lock_path.open("r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> Any:
        if not self.path.exists():
            return self.default()
        try:
            with self.path.open("r", encoding="utf-8") as state_file:
                value = json.load(state_file)
        except (json.JSONDecodeError, OSError) as exc:
            raise StateError(f"cannot read state file: {self.path}") from exc
        return value

    def _write_unlocked(self, value: Any) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
                json.dump(value, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass

    def load(self) -> Any:
        with self._lock():
            return self._read_unlocked()

    def save(self, value: Any) -> None:
        with self._lock():
            self._write_unlocked(value)

    def mutate(self, action: Callable[[Any], T]) -> T:
        with self._lock():
            value = self._read_unlocked()
            result = action(value)
            self._write_unlocked(value)
            return result


def safe_state_root(value: str | Path) -> Path:
    """Resolve a state directory and prevent accidental current-directory writes."""
    path = Path(value).expanduser().resolve()
    if str(path) in {"/", "."}:
        raise StateError("BOT_SERVER_DATA_DIR must be a dedicated directory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_or_create_state_key(data_dir: str | Path) -> str:
    """Load the persisted Fernet key, creating it exactly once on first boot."""
    root = Path(data_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    key_path = root / ".state_encryption_key"
    lock_path = root / ".state_encryption_key.lock"
    try:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                if key_path.exists():
                    key = key_path.read_text(encoding="ascii").strip()
                    CredentialVault(key)
                    os.chmod(key_path, 0o600)
                    return key

                # Never silently create a new key over existing encrypted bot
                # records; that would make their saved credentials unreadable.
                if (root / "talkin_bot_server.json").exists():
                    raise StateError(
                        "bot state exists but .state_encryption_key is missing; "
                        "restore the original key or set STATE_ENCRYPTION_KEY"
                    )

                key = CredentialVault.generate()
                fd, temp_name = tempfile.mkstemp(prefix=".state_encryption_key.", suffix=".tmp", dir=root)
                try:
                    with os.fdopen(fd, "w", encoding="ascii") as key_file:
                        key_file.write(key + "\n")
                        key_file.flush()
                        os.fsync(key_file.fileno())
                    os.chmod(temp_name, 0o600)
                    os.replace(temp_name, key_path)
                    os.chmod(key_path, 0o600)
                finally:
                    Path(temp_name).unlink(missing_ok=True)
                return key
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except StateError:
        raise
    except (OSError, UnicodeError) as exc:
        raise StateError(f"cannot load or create encryption key in {root}") from exc


def status_value(role: str, room: str, master: str, admin_name: str) -> str:
    """Build the requested Talkin-compatible coloured profile status."""
    role_label = "بوت متحكم" if str(role) == "controller" else "بوت صامت"
    return (
        '<B><H4><div style="background-color:#101827;padding:9px;text-align:center;">'
        f'<font color="#64D8FF">🤖 {role_label}</font><br>'
        f'<font color="#FFD166">👑 ادمن السيرفر: {admin_name}</font><br>'
        f'<font color="#A7F3D0">🏠 الغرفة: {room}</font><br>'
        f'<font color="#F9A8D4">👑 الماستر: {master}</font>'
        "</div></H4></B>"
    )


def normalize(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())
