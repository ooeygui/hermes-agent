"""Tests for plugins/blackboard — covers LocalBackend, ZenohBackend, core, watcher, tools.

All Zenoh tests mock eclipse-zenoh so no network or process is required.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Zenoh mock helpers (reusable across test classes)
# ---------------------------------------------------------------------------

def _make_sample(key_expr: str, payload_bytes: bytes):
    s = SimpleNamespace()
    s.key_expr = key_expr
    s.payload = SimpleNamespace(
        to_bytes=lambda: payload_bytes,
        to_string=lambda: payload_bytes.decode(),
    )
    return s


def _make_reply_ok(key_expr: str, payload_bytes: bytes):
    ok = SimpleNamespace(
        key_expr=key_expr,
        payload=SimpleNamespace(to_bytes=lambda: payload_bytes),
    )
    return SimpleNamespace(ok=ok)


def _build_zenoh_mock(session_get_returns=None):
    zenoh_mock = MagicMock()
    session_mock = MagicMock()
    zenoh_mock.open.return_value = session_mock
    zenoh_mock.Config.return_value = MagicMock()

    queryable_mock = MagicMock()
    ctx_m = MagicMock()
    ctx_m.__enter__ = MagicMock(return_value=None)   # None → break in loop
    ctx_m.__exit__ = MagicMock(return_value=False)
    queryable_mock.recv.return_value = ctx_m
    session_mock.declare_queryable.return_value = queryable_mock

    pub_mock = MagicMock()
    session_mock.declare_publisher.return_value = pub_mock
    session_mock.get.return_value = iter(session_get_returns or [])

    return zenoh_mock, session_mock, pub_mock


# ---------------------------------------------------------------------------
# BlackboardCache
# ---------------------------------------------------------------------------

class TestBlackboardCache:
    def _cache(self, tmp_path: Path):
        from plugins.blackboard.cache import BlackboardCache
        return BlackboardCache(tmp_path / "bb.db")

    def test_store_and_get_topic(self, tmp_path):
        c = self._cache(tmp_path)
        c.store_topic({"slug": "t1", "topic_name": "T1", "description": "d",
                       "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        assert c.get_topic("t1")["slug"] == "t1"

    def test_unknown_topic_returns_none(self, tmp_path):
        assert self._cache(tmp_path).get_topic("nope") is None

    def test_entries_deduped_by_id(self, tmp_path):
        c = self._cache(tmp_path)
        c.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                       "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        e = {"id": "e1", "content": "v1", "author": "a",
             "role": "c", "timestamp": "2025-01-01T00:01:00+00:00"}
        c.store_entry("t1", e)
        e["content"] = "v2"
        c.store_entry("t1", e)
        entries = c.get_entries("t1")
        assert len(entries) == 1
        assert entries[0]["content"] == "v2"

    def test_metadata_upsert(self, tmp_path):
        c = self._cache(tmp_path)
        c.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                       "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        c.store_metadata("t1", "status", {"value": "open", "updated_by": "a",
                                           "timestamp": "2025-01-01T00:00:00+00:00"})
        c.store_metadata("t1", "status", {"value": "closed", "updated_by": "a",
                                           "timestamp": "2025-01-01T00:01:00+00:00"})
        assert c.get_metadata("t1")["status"] == "closed"

    def test_get_matching_index(self, tmp_path):
        c = self._cache(tmp_path)
        c.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                       "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        results = c.get_matching_for_query("hermes/blackboard/index", "hermes/blackboard")
        assert len(results) == 1

    def test_get_matching_entries_slug(self, tmp_path):
        c = self._cache(tmp_path)
        c.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                       "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        c.store_entry("t1", {"id": "e1", "content": "hi", "author": "a",
                              "role": "c", "timestamp": "2025-01-01T00:01:00+00:00"})
        results = c.get_matching_for_query(
            "hermes/blackboard/entries/t1/**", "hermes/blackboard"
        )
        assert len(results) == 1
        assert json.loads(results[0][1])["id"] == "e1"

    def test_get_matching_wildcard_all(self, tmp_path):
        c = self._cache(tmp_path)
        c.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                       "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        c.store_entry("t1", {"id": "e1", "content": "hi", "author": "a",
                              "role": "c", "timestamp": "2025-01-01T00:01:00+00:00"})
        c.store_metadata("t1", "status", {"value": "open", "updated_by": "a",
                                           "timestamp": "2025-01-01T00:01:00+00:00"})
        results = c.get_matching_for_query("hermes/blackboard/**", "hermes/blackboard")
        keys = [r[0] for r in results]
        assert any("index" in k for k in keys)
        assert any("entries" in k for k in keys)
        assert any("meta" in k for k in keys)

    def test_get_entries_tail_returns_newest_ascending(self, tmp_path):
        """get_entries_tail should return the most recent N entries, oldest-first."""
        c = self._cache(tmp_path)
        c.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                       "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        # Store 5 entries with sequential timestamps
        for i in range(1, 6):
            c.store_entry("t1", {
                "id": f"e{i}",
                "content": f"entry {i}",
                "author": "a",
                "role": "c",
                "timestamp": f"2025-01-01T00:0{i}:00+00:00",
            })
        # get_entries_tail(limit=3) should return entries 3,4,5 in ASC order
        tail = c.get_entries_tail("t1", limit=3)
        assert len(tail) == 3
        assert tail[0]["id"] == "e3"
        assert tail[1]["id"] == "e4"
        assert tail[2]["id"] == "e5"
        # Verify ascending order (oldest first)
        assert tail[0]["timestamp"] < tail[1]["timestamp"] < tail[2]["timestamp"]

    def test_get_entries_tail_less_than_limit(self, tmp_path):
        """get_entries_tail returns all entries when count < limit."""
        c = self._cache(tmp_path)
        c.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                       "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        c.store_entry("t1", {"id": "e1", "content": "x", "author": "a",
                              "role": "c", "timestamp": "2025-01-01T00:01:00+00:00"})
        tail = c.get_entries_tail("t1", limit=10)
        assert len(tail) == 1


# ---------------------------------------------------------------------------
# LocalBackend
# ---------------------------------------------------------------------------

class TestLocalBackend:
    def test_publish_fires_subscriber_callback(self):
        from plugins.blackboard.backend import LocalBackend
        backend = LocalBackend()
        backend.open("hermes/blackboard")

        received = []
        handle = backend.subscribe(
            "hermes/blackboard/entries/t1/**",
            lambda s: received.append(json.loads(s.payload.to_bytes())),
        )

        backend.publish_entry("t1", {"id": "e1", "content": "hello"})
        time.sleep(0.1)   # background thread

        assert len(received) == 1
        assert received[0]["content"] == "hello"
        backend.unsubscribe(handle)

    def test_unsubscribe_stops_callbacks(self):
        from plugins.blackboard.backend import LocalBackend
        backend = LocalBackend()
        backend.open("hermes/blackboard")

        received = []
        handle = backend.subscribe(
            "hermes/blackboard/entries/t1/**",
            lambda s: received.append(1),
        )
        backend.unsubscribe(handle)
        backend.publish_entry("t1", {"id": "e1", "content": "after"})
        time.sleep(0.1)
        assert received == []

    def test_query_history_returns_empty(self):
        from plugins.blackboard.backend import LocalBackend
        b = LocalBackend()
        b.open("hermes/blackboard")
        assert b.query_history("hermes/blackboard/**", timeout=1.0) == []

    def test_wildcard_pattern_matching(self):
        from plugins.blackboard.backend import _key_matches
        assert _key_matches("hermes/blackboard/**", "hermes/blackboard/entries/t1/e1")
        assert _key_matches("hermes/blackboard/entries/t1/**", "hermes/blackboard/entries/t1/e1")
        assert not _key_matches("hermes/blackboard/entries/t2/**", "hermes/blackboard/entries/t1/e1")
        assert _key_matches("hermes/blackboard/index", "hermes/blackboard/index")
        assert not _key_matches("hermes/blackboard/index", "hermes/blackboard/entries/t1/e1")

    def test_no_subscribers_no_thread_spawned(self):
        """publish with no subscribers must not raise."""
        from plugins.blackboard.backend import LocalBackend
        b = LocalBackend()
        b.open("hermes/blackboard")
        b.publish_entry("t1", {"id": "e1", "content": "x"})  # should not raise

    def test_close_clears_subs(self):
        from plugins.blackboard.backend import LocalBackend
        b = LocalBackend()
        b.open("ns")
        b.subscribe("ns/**", lambda s: None)
        b.close()
        assert b._subs == {}


# ---------------------------------------------------------------------------
# ZenohBackend
# ---------------------------------------------------------------------------

class TestZenohBackend:
    def test_open_starts_queryable_thread(self, tmp_path, monkeypatch):
        zenoh_mock, session_mock, pub_mock = _build_zenoh_mock()
        monkeypatch.setitem(sys.modules, "zenoh", zenoh_mock)

        from plugins.blackboard.backends.zenoh import ZenohBackend
        b = ZenohBackend()
        b.open("hermes/blackboard")

        session_mock.declare_queryable.assert_called_once()
        assert b._query_thread is not None
        b.close()

    def test_publish_entry_puts_json(self, tmp_path, monkeypatch):
        zenoh_mock, session_mock, pub_mock = _build_zenoh_mock()
        monkeypatch.setitem(sys.modules, "zenoh", zenoh_mock)

        from plugins.blackboard.backends.zenoh import ZenohBackend
        b = ZenohBackend()
        b.open("hermes/blackboard")
        b.publish_entry("t1", {"id": "e1", "content": "hello"})

        pub_mock.put.assert_called()
        raw = pub_mock.put.call_args[0][0]
        assert json.loads(raw)["content"] == "hello"
        b.close()

    def test_query_history_deduplicates(self, monkeypatch):
        entry = {"id": "e1", "content": "c"}
        replies = [
            _make_reply_ok("ns/entries/t1/e1", json.dumps(entry).encode()),
            _make_reply_ok("ns/entries/t1/e1", json.dumps(entry).encode()),
        ]
        zenoh_mock, session_mock, _ = _build_zenoh_mock(session_get_returns=replies)
        session_mock.get.return_value = iter(replies)
        monkeypatch.setitem(sys.modules, "zenoh", zenoh_mock)

        from plugins.blackboard.backends.zenoh import ZenohBackend
        b = ZenohBackend()
        b.open("ns")
        results = b.query_history("ns/entries/t1/**", timeout=1.0)

        assert len(results) == 1
        b.close()

    def test_close_undeclares_queryable(self, monkeypatch):
        zenoh_mock, session_mock, _ = _build_zenoh_mock()
        monkeypatch.setitem(sys.modules, "zenoh", zenoh_mock)

        from plugins.blackboard.backends.zenoh import ZenohBackend
        b = ZenohBackend()
        b.open("hermes/blackboard")
        b.close()

        session_mock.close.assert_called_once()


# ---------------------------------------------------------------------------
# BlackboardCore
# ---------------------------------------------------------------------------

class TestBlackboardCore:
    def _make_core(self, tmp_path, backend=None):
        from plugins.blackboard.cache import BlackboardCache
        from plugins.blackboard.backend import LocalBackend
        from plugins.blackboard.core import BlackboardCore
        cache = BlackboardCache(tmp_path / "bb.db")
        b = backend or LocalBackend()
        b.open("hermes/blackboard")
        return BlackboardCore(cache=cache, backend=b, namespace="hermes/blackboard"), cache, b

    def test_create_topic_stored_in_cache(self, tmp_path):
        core, cache, _ = self._make_core(tmp_path)
        core.create_topic("t1", "T One", "desc", "agent-a")
        assert cache.get_topic("t1") is not None

    def test_write_entry_stored_in_cache(self, tmp_path):
        core, cache, _ = self._make_core(tmp_path)
        core.create_topic("t1", "T", "", "a")
        core.write_entry("t1", "hello", "agent-a")
        entries = cache.get_entries("t1")
        assert len(entries) == 1
        assert entries[0]["content"] == "hello"

    def test_get_topic_returns_entries_and_metadata(self, tmp_path):
        core, _, _ = self._make_core(tmp_path)
        core.create_topic("t1", "T", "", "a")
        core.write_entry("t1", "entry1", "agent-a")
        core.update_metadata("t1", "status", "active", "agent-a")
        result = core.get_topic("t1")
        assert result is not None
        assert len(result["entries"]) == 1
        assert result["metadata"]["status"] == "active"

    def test_get_topic_returns_none_for_missing(self, tmp_path):
        core, _, _ = self._make_core(tmp_path)
        assert core.get_topic("missing") is None

    def test_write_entry_fires_local_subscriber(self, tmp_path):
        from plugins.blackboard.backend import LocalBackend
        backend = LocalBackend()
        core, _, _ = self._make_core(tmp_path, backend=backend)
        core.create_topic("t1", "T", "", "a")

        received = []
        handle = backend.subscribe(
            "hermes/blackboard/entries/t1/**",
            lambda s: received.append(json.loads(s.payload.to_bytes())),
        )
        core.write_entry("t1", "ping", "agent-a")
        time.sleep(0.1)

        assert len(received) == 1
        assert received[0]["content"] == "ping"
        backend.unsubscribe(handle)

    def test_list_topics_uses_cache_when_backend_empty(self, tmp_path):
        core, _, _ = self._make_core(tmp_path)
        core.create_topic("t1", "T", "", "a")
        core.create_topic("t2", "T2", "", "a")
        topics = core.list_topics(timeout=0.1)
        assert len(topics) == 2


# ---------------------------------------------------------------------------
# BlackboardWatcher
# ---------------------------------------------------------------------------

class TestBlackboardWatcher:
    def test_watch_and_poll(self, tmp_path):
        from plugins.blackboard.backend import LocalBackend
        from plugins.blackboard.cache import BlackboardCache
        from plugins.blackboard.core import BlackboardCore
        from plugins.blackboard.watcher import BlackboardWatcher

        backend = LocalBackend()
        backend.open("hermes/blackboard")
        cache = BlackboardCache(tmp_path / "bb.db")
        core = BlackboardCore(cache=cache, backend=backend, namespace="hermes/blackboard")
        watcher = BlackboardWatcher()

        core.create_topic("t1", "T1", "", "a")
        watcher.watch_topic("sess-1", "t1", "T1", backend, "hermes/blackboard")

        core.write_entry("t1", "hello from agent", "agent-b")
        time.sleep(0.15)

        notifications = watcher.poll("sess-1")
        assert len(notifications) == 1
        assert notifications[0].entry["content"] == "hello from agent"

    def test_watch_twice_returns_false(self, tmp_path):
        from plugins.blackboard.backend import LocalBackend
        from plugins.blackboard.watcher import BlackboardWatcher
        b = LocalBackend(); b.open("ns")
        w = BlackboardWatcher()
        assert w.watch_topic("s", "t1", "T", b, "ns") is True
        assert w.watch_topic("s", "t1", "T", b, "ns") is False

    def test_deregister_clears_queue(self):
        from plugins.blackboard.backend import LocalBackend
        from plugins.blackboard.watcher import BlackboardWatcher
        b = LocalBackend(); b.open("ns")
        w = BlackboardWatcher()
        w.register_session("s")
        w.deregister_session("s", backend=b)
        assert w.poll("s") == []

    def test_format_notifications(self):
        from plugins.blackboard.watcher import BlackboardWatcher, Notification
        w = BlackboardWatcher()
        n = Notification("t1", "Topic One", {"content": "great finding", "author": "agent-x"})
        out = w.format_notifications([n])
        assert "Topic One" in out
        assert "agent-x" in out
        assert "great finding" in out


# ---------------------------------------------------------------------------
# Tool handlers (full integration with LocalBackend)
# ---------------------------------------------------------------------------

class TestToolHandlers:
    @pytest.fixture(autouse=True)
    def _reset_singletons(self, tmp_path, monkeypatch):
        import plugins.blackboard as bb
        monkeypatch.setattr(bb, "_core", None)
        monkeypatch.setattr(bb, "_watcher", None)
        monkeypatch.setattr(bb, "_backend_instance", None)
        monkeypatch.setattr(bb, "_plugin_config", {
            "backend": "local",
            "cache_db_path": str(tmp_path / "bb.db"),
        })
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    def test_create_topic(self):
        import plugins.blackboard as bb
        r = json.loads(bb._handle_create_topic(
            {"slug": "research", "name": "Research"}, agent_identity="a"
        ))
        assert r["success"] is True
        assert r["topic"]["slug"] == "research"

    def test_create_duplicate_rejected(self):
        import plugins.blackboard as bb
        bb._handle_create_topic({"slug": "t1"}, agent_identity="a")
        r = json.loads(bb._handle_create_topic({"slug": "t1"}, agent_identity="a"))
        assert "error" in r
        assert "already exists" in r["error"]

    def test_invalid_slug_rejected(self):
        import plugins.blackboard as bb
        r = json.loads(bb._handle_create_topic({"slug": "bad slug!"}, agent_identity="a"))
        assert "error" in r

    def test_write_entry_requires_topic(self):
        import plugins.blackboard as bb
        bb._ensure_initialized()
        r = json.loads(bb._handle_write_entry({"slug": "nope", "content": "hi"}, agent_identity="a"))
        assert "error" in r

    def test_write_entry_success(self):
        import plugins.blackboard as bb
        bb._handle_create_topic({"slug": "t1"}, agent_identity="a")
        r = json.loads(bb._handle_write_entry({"slug": "t1", "content": "hello"}, agent_identity="a"))
        assert r["success"] is True
        assert r["entry"]["content"] == "hello"

    def test_list_topics(self):
        import plugins.blackboard as bb
        bb._handle_create_topic({"slug": "t1", "name": "One"}, agent_identity="a")
        bb._handle_create_topic({"slug": "t2", "name": "Two"}, agent_identity="a")
        r = json.loads(bb._handle_list_topics({}))
        assert r["count"] == 2

    def test_get_topic_not_found(self):
        import plugins.blackboard as bb
        bb._ensure_initialized()
        r = json.loads(bb._handle_get_topic({"slug": "missing"}))
        assert "error" in r

    def test_get_topic_with_entries(self):
        import plugins.blackboard as bb
        bb._handle_create_topic({"slug": "t1"}, agent_identity="a")
        bb._handle_write_entry({"slug": "t1", "content": "hi"}, agent_identity="a")
        r = json.loads(bb._handle_get_topic({"slug": "t1"}))
        assert r["success"] is True
        assert len(r["entries"]) == 1

    def test_update_metadata_invalid_key(self):
        import plugins.blackboard as bb
        bb._handle_create_topic({"slug": "t1"}, agent_identity="a")
        r = json.loads(bb._handle_update_metadata(
            {"slug": "t1", "key": "bad key!", "value": "x"}, agent_identity="a"
        ))
        assert "error" in r

    def test_update_metadata_success(self):
        import plugins.blackboard as bb
        bb._handle_create_topic({"slug": "t1"}, agent_identity="a")
        r = json.loads(bb._handle_update_metadata(
            {"slug": "t1", "key": "status", "value": "active"}, agent_identity="a"
        ))
        assert r["success"] is True

    def test_watch_and_poll_end_to_end(self):
        import plugins.blackboard as bb
        bb._handle_create_topic({"slug": "t1", "name": "T1"}, agent_identity="a")
        r = json.loads(bb._handle_watch({"slug": "t1"}, task_id="sess-1"))
        assert r["success"] is True

        # write an entry — LocalBackend fires subscriber callback in background thread
        bb._handle_write_entry({"slug": "t1", "content": "update"}, agent_identity="agent-b")
        time.sleep(0.15)

        poll = json.loads(bb._handle_poll({}, task_id="sess-1"))
        assert poll["count"] == 1
        assert "update" in poll["summary"]

    def test_watch_nonexistent_topic_rejected(self):
        import plugins.blackboard as bb
        bb._ensure_initialized()
        r = json.loads(bb._handle_watch({"slug": "ghost"}, task_id="s"))
        assert "error" in r

    def test_poll_returns_empty_before_watch(self):
        import plugins.blackboard as bb
        bb._ensure_initialized()
        r = json.loads(bb._handle_poll({}, task_id="sess-new"))
        assert r["count"] == 0



