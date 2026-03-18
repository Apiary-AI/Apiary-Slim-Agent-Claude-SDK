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
