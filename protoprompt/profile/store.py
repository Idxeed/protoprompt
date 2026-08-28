"""Key-value persistence for :class:`~protoprompt.profile.types.UserProfile`.

Profiles are stored as whole documents (not vectors): reading is by exact
``user_id``, which is the only access pattern the profile engine needs.
Serialization is JSON, so both built-in stores share the same codec.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from typing import Protocol, runtime_checkable

from protoprompt.profile.types import Preferences, Traits, UserProfile


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
    """Throwaway profile store for tests and short-lived processes."""

    def __init__(self) -> None:
        self._data: dict[str, UserProfile] = {}
        self._lock = threading.Lock()

    def get(self, user_id: str) -> UserProfile | None:
        with self._lock:
            return self._data.get(user_id)

    def put(self, profile: UserProfile) -> None:
        with self._lock:
            self._data[profile.user_id] = profile

    def delete(self, user_id: str) -> None:
        with self._lock:
            self._data.pop(user_id, None)

    def compare_and_put(
        self, profile: UserProfile, *, expected_version: int | None
    ) -> bool:
        with self._lock:
            current = self._data.get(profile.user_id)
            if expected_version is None:
                if current is not None:
                    return False
            elif current is None or current.version != expected_version:
                return False
            self._data[profile.user_id] = profile
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

    def get(self, user_id: str) -> UserProfile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        return profile_from_dict(json.loads(row[0]))

    def put(self, profile: UserProfile) -> None:
        payload = json.dumps(profile_to_dict(profile), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO profiles (user_id, json, version) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET json = excluded.json, "
                "version = excluded.version",
                (profile.user_id, payload, profile.version),
            )
            self._conn.commit()

    def compare_and_put(
        self, profile: UserProfile, *, expected_version: int | None
    ) -> bool:
        payload = json.dumps(profile_to_dict(profile), ensure_ascii=False)
        with self._lock:
            if expected_version is None:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO profiles (user_id, json, version) "
                    "VALUES (?, ?, ?)",
                    (profile.user_id, payload, profile.version),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE profiles SET json = ?, version = ? "
                    "WHERE user_id = ? AND version = ?",
                    (payload, profile.version, profile.user_id, expected_version),
                )
            self._conn.commit()
            return cursor.rowcount == 1

    def delete(self, user_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))
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
