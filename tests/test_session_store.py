import pytest

from src.session_store import SessionStore


def test_get_before_set_returns_none(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    assert store.get("chat1") is None


def test_set_then_get(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set("chat1", "session-abc")
    assert store.get("chat1") == "session-abc"


def test_chat_id_int_and_str_are_equivalent(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set(123, "session-xyz")
    assert store.get(123) == "session-xyz"
    assert store.get("123") == "session-xyz"


def test_clear_removes_entry(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set("chat1", "session-abc")
    store.clear("chat1")
    assert store.get("chat1") is None


def test_clear_nonexistent_is_safe(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.clear("nonexistent")  # must not raise


def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "sessions.json")
    store1 = SessionStore(path)
    store1.set("chat1", "session-abc")

    store2 = SessionStore(path)
    assert store2.get("chat1") == "session-abc"


def test_corrupt_json_starts_fresh(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("not valid json{{{")
    store = SessionStore(str(path))
    assert store.get("chat1") is None


def test_set_with_version_then_get_with_version(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set_with_version("chat1", "session-abc", 3)
    assert store.get_with_version("chat1") == ("session-abc", 3)
    # plain get still works
    assert store.get("chat1") == "session-abc"


def test_get_with_version_missing_returns_none(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    assert store.get_with_version("nope") is None


def test_legacy_plain_string_loads_as_version_zero(tmp_path):
    """Old session files (pre-versioning) stored plain session_id strings."""
    path = tmp_path / "sessions.json"
    import json as _json
    path.write_text(_json.dumps({"chat1": "legacy-session"}))
    store = SessionStore(str(path))
    assert store.get_with_version("chat1") == ("legacy-session", 0)


def test_invalidate_older_than_drops_stale_only(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set_with_version("chat_old", "s1", 1)
    store.set_with_version("chat_current", "s2", 5)
    store.set_with_version("chat_future", "s3", 7)
    dropped = store.invalidate_older_than(5)
    assert dropped == 1
    assert store.get("chat_old") is None
    assert store.get("chat_current") == "s2"
    assert store.get("chat_future") == "s3"


def test_invalidate_preserves_version_none_entries(tmp_path):
    """Entries with no version (legacy set()) are kept — no basis to compare."""
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set("chat_unknown", "s1")  # version=None
    dropped = store.invalidate_older_than(10)
    assert dropped == 0
    assert store.get("chat_unknown") == "s1"


def test_version_persists_across_instances(tmp_path):
    path = str(tmp_path / "sessions.json")
    store1 = SessionStore(path)
    store1.set_with_version("chat1", "session-abc", 2)
    store2 = SessionStore(path)
    assert store2.get_with_version("chat1") == ("session-abc", 2)
