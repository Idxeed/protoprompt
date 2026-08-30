"""Key-value persistence for :class:`~protoprompt.profile.types.UserProfile`.

Profiles are stored as whole documents (not vectors): reading is by exact
logical ``user_id``, which is the only access pattern the profile engine
needs.  Built-in stores can additionally derive an isolated physical key from
a :class:`~protoprompt.scope.MemoryScope` while preserving that logical id in
the returned and serialized :class:`~protoprompt.profile.types.UserProfile`.
Serialization is JSON, so both built-in stores share the same codec.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from typing import Protocol, runtime_checkable

from protoprompt.profile.types import Preferences, Traits, UserProfile
from protoprompt.scope import MemoryScope, scoped_doc_id


def _storage_user_id(user_id: str, scope: MemoryScope | None) -> str:
    """Return the physical profile key without changing its logical id.

    Empty and omitted scopes deliberately keep the legacy storage layout.
    """
    if scope is None or not scope.has_identity:
        return user_id
    return scoped_doc_id(user_id, scope)


def _scoped_read_profile(
    profile: UserProfile | None,
    user_id: str,
    scope: MemoryScope | None,
) -> UserProfile | None:
    """Reject a legacy physical-key collision during a scoped read.

    A pre-scope profile can legitimately have the same literal id as a new
    scoped storage key.  Such a record is not proof that it belongs to the
    requested logical user, so it must not be returned through a scoped read.
    Unscoped reads intentionally retain their legacy behavior.
    """
    if (
        profile is not None
        and scope is not None
        and scope.has_identity
        and profile.user_id != user_id
    ):
        return None
    return profile


def _assert_scoped_owner(
    current: UserProfile | None,
    user_id: str,
    scope: MemoryScope | None,
) -> None:
    """Fail closed before a scoped mutator overwrites a legacy collision.

    A scoped read treats a mismatched logical id at its derived physical key as
    absent.  Writes, deletes, and compare-and-swap must make the same decision:
    otherwise a reset or erase operation could destroy an unrelated legacy
    record that was correctly hidden from the scoped reader.
    """
    if (
        current is not None
        and scope is not None
        and scope.has_identity
        and current.user_id != user_id
    ):
        raise ValueError(
            "scoped profile key is owned by a different logical user; "
            "refusing to overwrite or delete it"
        )


def profile_to_dict(profile: UserProfile) -> dict:
    return asdict(profile)


def profile_from_dict(data: dict) -> UserProfile:
    raw_traits = data.get("traits", {})
    raw_preferences = data.get("preferences", {})
    traits_data = raw_traits if isinstance(raw_traits, dict) else {}
    preferences_data = raw_preferences if isinstance(raw_preferences, dict) else {}
    topics = preferences_data.get("topics", [])
    facts = data.get("facts", {})
    try:
        version = int(data.get("version", 0))
    except (TypeError, ValueError):
        version = 0
    return UserProfile(
        user_id=str(data.get("user_id", "")),
        traits=Traits(**{
            key: str(traits_data.get(key, ""))
            for key in ("style", "expertise", "verbosity", "formality")
        }),
        preferences=Preferences(
            format=str(preferences_data.get("format", "")),
            language=str(preferences_data.get("language", "")),
            topics=[str(item) for item in topics] if isinstance(topics, list) else [],
        ),
        facts={str(key): str(value) for key, value in facts.items()}
        if isinstance(facts, dict)
        else {},
        summary=str(data.get("summary", "")),
        updated_at=str(data.get("updated_at", "")),
        version=version,
        source=str(data.get("source", "")),
    )


@runtime_checkable
class ProfileStore(Protocol):
    def get(self, user_id: str) -> UserProfile | None:
        ...

    def put(self, profile: UserProfile) -> None:
        ...

    def delete(self, user_id: str) -> None:
        ...

    def compare_and_put(
        self, profile: UserProfile, *, expected_version: int | None
    ) -> bool:
        """Persist only when the current version matches the expectation."""
        ...


class InMemoryProfileStore:
    """Throwaway profile store for tests and short-lived processes.

    Pass ``scope`` to any operation to isolate physical storage keys.  The
    :class:`UserProfile` itself always keeps its logical ``user_id``.
    """

    supports_profile_scopes = True

    def __init__(self) -> None:
        self._data: dict[str, UserProfile] = {}
        self._lock = threading.Lock()

    def get(
        self,
        user_id: str,
        *,
        scope: MemoryScope | None = None,
    ) -> UserProfile | None:
        with self._lock:
            profile = self._data.get(_storage_user_id(user_id, scope))
        return _scoped_read_profile(profile, user_id, scope)

    def put(self, profile: UserProfile, *, scope: MemoryScope | None = None) -> None:
        with self._lock:
            storage_id = _storage_user_id(profile.user_id, scope)
            _assert_scoped_owner(self._data.get(storage_id), profile.user_id, scope)
            self._data[storage_id] = profile

    def delete(self, user_id: str, *, scope: MemoryScope | None = None) -> None:
        with self._lock:
            storage_id = _storage_user_id(user_id, scope)
            _assert_scoped_owner(self._data.get(storage_id), user_id, scope)
            self._data.pop(storage_id, None)

    def compare_and_put(
        self,
        profile: UserProfile,
        *,
        expected_version: int | None,
        scope: MemoryScope | None = None,
    ) -> bool:
        storage_id = _storage_user_id(profile.user_id, scope)
        with self._lock:
            current = self._data.get(storage_id)
            _assert_scoped_owner(current, profile.user_id, scope)
            if expected_version is None:
                if current is not None:
                    return False
            elif current is None or current.version != expected_version:
                return False
            self._data[storage_id] = profile
            return True


_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    user_id TEXT PRIMARY KEY,
    json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);
"""


class SqliteProfileStore:
    """Persistent profile store, standard library only.

    Args:
        path: SQLite file path, or ``":memory:"``.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(profiles)")
            }
            if "version" not in columns:
                self._conn.execute(
                    "ALTER TABLE profiles ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
                )
                for user_id, payload in self._conn.execute(
                    "SELECT user_id, json FROM profiles"
                ).fetchall():
                    try:
                        stored_version = int(json.loads(payload).get("version", 0))
                    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                        stored_version = 0
                    self._conn.execute(
                        "UPDATE profiles SET version = ? WHERE user_id = ?",
                        (stored_version, user_id),
                    )
            self._conn.commit()

    supports_profile_scopes = True

    def get(
        self,
        user_id: str,
        *,
        scope: MemoryScope | None = None,
    ) -> UserProfile | None:
        storage_id = _storage_user_id(user_id, scope)
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM profiles WHERE user_id = ?", (storage_id,)
            ).fetchone()
        if row is None:
            return None
        return _scoped_read_profile(profile_from_dict(json.loads(row[0])), user_id, scope)

    def put(self, profile: UserProfile, *, scope: MemoryScope | None = None) -> None:
        storage_id = _storage_user_id(profile.user_id, scope)
        payload = json.dumps(profile_to_dict(profile), ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM profiles WHERE user_id = ?", (storage_id,)
            ).fetchone()
            if row is not None:
                _assert_scoped_owner(
                    profile_from_dict(json.loads(row[0])), profile.user_id, scope
                )
            self._conn.execute(
                "INSERT INTO profiles (user_id, json, version) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET json = excluded.json, "
                "version = excluded.version",
                (storage_id, payload, profile.version),
            )
            self._conn.commit()

    def compare_and_put(
        self,
        profile: UserProfile,
        *,
        expected_version: int | None,
        scope: MemoryScope | None = None,
    ) -> bool:
        storage_id = _storage_user_id(profile.user_id, scope)
        payload = json.dumps(profile_to_dict(profile), ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM profiles WHERE user_id = ?", (storage_id,)
            ).fetchone()
            if row is not None:
                _assert_scoped_owner(
                    profile_from_dict(json.loads(row[0])), profile.user_id, scope
                )
            if expected_version is None:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO profiles (user_id, json, version) "
                    "VALUES (?, ?, ?)",
                    (storage_id, payload, profile.version),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE profiles SET json = ?, version = ? "
                    "WHERE user_id = ? AND version = ?",
                    (payload, profile.version, storage_id, expected_version),
                )
            self._conn.commit()
            return cursor.rowcount == 1

    def delete(self, user_id: str, *, scope: MemoryScope | None = None) -> None:
        storage_id = _storage_user_id(user_id, scope)
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM profiles WHERE user_id = ?", (storage_id,)
            ).fetchone()
            if row is not None:
                _assert_scoped_owner(
                    profile_from_dict(json.loads(row[0])), user_id, scope
                )
            self._conn.execute("DELETE FROM profiles WHERE user_id = ?", (storage_id,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SqliteProfileStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        conn = getattr(self, "_conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
