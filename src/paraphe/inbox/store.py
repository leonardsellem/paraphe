"""Store location and SQLite card payload for the Inbox."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

STORE_DIR_NAME = "paraphe"
STORE_FILENAME = "inbox.sqlite"
STORE_MODE = 0o600
DATA_DIR_MODE = 0o700
# The location earlier releases used, and the one an existing deployment still
# configures. Resolving somewhere else while this one holds a store refuses
# rather than starting a second, empty store.
LEGACY_STORE_PATH = Path("/var/lib/paraphe/inbox.sqlite")


def default_data_dir() -> Path:
    """The platform's per-user data directory for this product."""
    base = os.environ.get("XDG_DATA_HOME")
    if base and base.strip():
        return Path(base).expanduser() / STORE_DIR_NAME
    return Path.home() / ".local" / "share" / STORE_DIR_NAME


def default_store_path() -> Path:
    return default_data_dir() / STORE_FILENAME


def store_exists(path: Path) -> bool:
    """True when a store file is there. An unreadable path has no store to strand."""
    try:
        return path.is_file()
    except OSError:
        return False


def _setup_error(message: str) -> Exception:
    from .config import SetupError

    return SetupError(message)


def relocate_store(
    source: Path | str,
    target: Path | str,
    *,
    move: bool = False,
) -> Path:
    """Copy a SQLite store safely, optionally removing the source afterward."""
    source_path = Path(source)
    target_path = Path(target)

    # 1. Refuse before changing the filesystem if the source is missing, the
    # target is occupied, or both names resolve to the same location. A probe
    # that cannot read the path — a service-owned legacy directory, say — must
    # answer with a message naming it rather than raise out of the command.
    try:
        source_found = source_path.is_file()
    except OSError as exc:
        raise _setup_error(f"source store cannot be read: {source_path}") from exc
    try:
        target_found = target_path.exists()
    except OSError as exc:
        raise _setup_error(f"target store cannot be read: {target_path}") from exc
    if not source_found:
        raise _setup_error(f"source store does not exist: {source_path}")
    if target_found:
        raise _setup_error(f"target store already exists: {target_path}")
    if source_path.resolve() == target_path.resolve():
        raise _setup_error("source and target store are the same")

    # 2. Prepare the destination directory with owner-only permissions. Do not
    # create the final database path yet: startup must never see a partial copy.
    parent = target_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=DATA_DIR_MODE)
        os.chmod(parent, DATA_DIR_MODE)
    except OSError as exc:
        raise _setup_error(f"data directory is not usable: {parent}") from exc

    # 3. Build the copy under a temporary name in the destination directory.
    # Keeping both files on one filesystem lets the final installation be atomic.
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            dir=parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)

        # 4. Let SQLite produce a consistent snapshot instead of copying the
        # database bytes while a transaction or journal may be active.
        source_connection = sqlite3.connect(
            source_path.resolve().as_uri() + "?mode=ro",
            uri=True,
        )
        try:
            target_connection = sqlite3.connect(temporary_path)
            try:
                source_connection.backup(target_connection)
            finally:
                target_connection.close()
        finally:
            source_connection.close()

        # 5. Verify the completed snapshot before exposing it as the real store.
        # The integrity check alone passes an empty or foreign database, which
        # would install a store that is not the owner's data — so the snapshot
        # must also carry the table every store has carried.
        verification = sqlite3.connect(temporary_path)
        try:
            result = verification.execute("PRAGMA integrity_check").fetchone()
            tables = {
                row[0]
                for row in verification.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            verification.close()
        if result != ("ok",):
            raise _setup_error("copied store failed its integrity check")
        if "cards" not in tables:
            raise _setup_error(f"source store is not a Paraphe store: {source_path}")

        # 6. Secure the file, then link it into place. os.link refuses if the
        # target appeared meanwhile, so a concurrent file is never overwritten.
        temporary_path.chmod(STORE_MODE)
        target_linked = False
        try:
            os.link(temporary_path, target_path)
            target_linked = True
            target_path.chmod(STORE_MODE)
            temporary_path.unlink()
        except FileExistsError as exc:
            raise _setup_error(f"target store already exists: {target_path}") from exc
        except OSError:
            if target_linked:
                try:
                    target_path.unlink()
                except OSError:
                    pass
            raise
        temporary_path = None

        # 7. Copying is the safe default. In move mode, remove the source only
        # after the verified target exists; roll the target back if removal fails.
        if move:
            try:
                source_path.unlink()
            except OSError as exc:
                try:
                    target_path.unlink()
                except OSError:
                    pass
                raise _setup_error(f"could not remove source store: {source_path}") from exc
    except sqlite3.Error as exc:
        raise _setup_error(f"store could not be copied: {source_path}") from exc
    except OSError as exc:
        raise _setup_error(f"store could not be relocated: {source_path}") from exc
    finally:
        # 8. Any failure before installation removes the unfinished snapshot.
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    return target_path


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_store_path()
        self._conn: sqlite3.Connection | None = None

    @classmethod
    def prepare(cls, path: Path | str) -> Path:
        target = Path(path)
        parent = target.parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True, mode=DATA_DIR_MODE)
                os.chmod(parent, DATA_DIR_MODE)
            except OSError as exc:
                raise _setup_error(f"data directory is not usable: {parent}") from exc
        try:
            target.touch(exist_ok=True)
            target.chmod(STORE_MODE)
        except OSError as exc:
            raise _setup_error(f"store file is not usable: {target}") from exc
        return target

    def open(self) -> None:
        self.prepare(self.path)
        # ponytail: serialized sqlite, split connections if writers contend
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cards ("
            "request_id TEXT PRIMARY KEY, "
            "payload TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS notifications ("
            "external_id TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS durable_state ("
            "key TEXT PRIMARY KEY, "
            "value TEXT NOT NULL)"
        )
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def load_cards(self) -> list[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("store is not open")
        rows = self._conn.execute("SELECT payload FROM cards").fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_card(self, payload: dict[str, Any]) -> None:
        if self._conn is None:
            raise RuntimeError("store is not open")
        self._conn.execute(
            "INSERT OR REPLACE INTO cards (request_id, payload) VALUES (?, ?)",
            (payload["request_id"], json.dumps(payload)),
        )
        self._conn.commit()

    def load_notification_ids(self) -> list[str]:
        if self._conn is None:
            raise RuntimeError("store is not open")
        rows = self._conn.execute("SELECT external_id FROM notifications").fetchall()
        return [str(row[0]) for row in rows]

    def save_notification_id(self, external_id: str) -> None:
        if self._conn is None:
            raise RuntimeError("store is not open")
        self._conn.execute(
            "INSERT OR REPLACE INTO notifications (external_id) VALUES (?)",
            (external_id,),
        )
        self._conn.commit()

    def delete_notification_id(self, external_id: str) -> None:
        if self._conn is None:
            raise RuntimeError("store is not open")
        self._conn.execute(
            "DELETE FROM notifications WHERE external_id = ?",
            (external_id,),
        )
        self._conn.commit()

    def load_telegram_next_offset(self) -> int | None:
        if self._conn is None:
            raise RuntimeError("store is not open")
        row = self._conn.execute(
            "SELECT value FROM durable_state WHERE key = ?",
            ("telegram_next_offset",),
        ).fetchone()
        if row is None:
            return None
        try:
            offset = int(row[0])
        except (TypeError, ValueError):
            raise RuntimeError("telegram next offset is invalid") from None
        if offset < 0:
            raise RuntimeError("telegram next offset is invalid")
        return offset

    def save_telegram_next_offset(self, offset: int) -> None:
        if self._conn is None:
            raise RuntimeError("store is not open")
        if offset < 0:
            raise RuntimeError("telegram next offset is invalid")
        self._conn.execute(
            "INSERT OR REPLACE INTO durable_state (key, value) VALUES (?, ?)",
            ("telegram_next_offset", str(offset)),
        )
        self._conn.commit()
