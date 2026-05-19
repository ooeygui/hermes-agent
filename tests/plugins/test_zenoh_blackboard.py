"""Tests for the zenoh_blackboard plugin.

All tests mock the `zenoh` module so no network or extra process is needed.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to build a minimal mock of the eclipse-zenoh Python API
# ---------------------------------------------------------------------------

def _make_sample(key_expr: str, payload_bytes: bytes):
    sample = SimpleNamespace()
    sample.key_expr = key_expr
    sample.payload = SimpleNamespace()
    sample.payload.to_bytes = lambda: payload_bytes
    sample.payload.to_string = lambda: payload_bytes.decode()
    return sample


def _make_reply_ok(key_expr: str, payload_bytes: bytes):
    ok = SimpleNamespace()
    ok.key_expr = key_expr
    ok.payload = SimpleNamespace()
    ok.payload.to_bytes = lambda: payload_bytes
    reply = SimpleNamespace()
    reply.ok = ok
    return reply


# ---------------------------------------------------------------------------
# Cache tests — no zenoh dependency
# ---------------------------------------------------------------------------

class TestBlackboardCache:
    def _make_cache(self, tmp_path: Path):
        from plugins.zenoh_blackboard.cache import BlackboardCache
        return BlackboardCache(tmp_path / "test_bb.db")

    def test_store_and_retrieve_topic(self, tmp_path):
        cache = self._make_cache(tmp_path)
        cache.store_topic({"slug": "t1", "topic_name": "Test 1", "description": "desc",
                           "created_by": "agent-a", "created_at": "2025-01-01T00:00:00+00:00"})
        topic = cache.get_topic("t1")
        assert topic is not None
        assert topic["slug"] == "t1"
        assert topic["name"] == "Test 1"

    def test_store_and_retrieve_entries(self, tmp_path):
        cache = self._make_cache(tmp_path)
        cache.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                           "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        cache.store_entry("t1", {"id": "e1", "content": "hello", "author": "agent-a",
                                  "role": "contributor", "timestamp": "2025-01-01T00:01:00+00:00"})
        entries = cache.get_entries("t1")
        assert len(entries) == 1
        assert entries[0]["content"] == "hello"

    def test_store_and_retrieve_metadata(self, tmp_path):
        cache = self._make_cache(tmp_path)
        cache.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                           "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        cache.store_metadata("t1", "status", {"value": "active", "updated_by": "x",
                                               "timestamp": "2025-01-01T00:02:00+00:00"})
        meta = cache.get_metadata("t1")
        assert meta["status"] == "active"

    def test_entry_dedup_via_upsert(self, tmp_path):
        cache = self._make_cache(tmp_path)
        cache.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                           "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        entry = {"id": "e1", "content": "first", "author": "a",
                 "role": "c", "timestamp": "2025-01-01T00:01:00+00:00"}
        cache.store_entry("t1", entry)
        # Update content via upsert
        entry["content"] = "updated"
        cache.store_entry("t1", entry)
        entries = cache.get_entries("t1")
        assert len(entries) == 1
        assert entries[0]["content"] == "updated"

    def test_get_matching_for_query_index(self, tmp_path):
        cache = self._make_cache(tmp_path)
        cache.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                           "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        results = cache.get_matching_for_query("hermes/blackboard/index", "hermes/blackboard")
        assert len(results) == 1
        key, payload = results[0]
        assert key == "hermes/blackboard/index"
        data = json.loads(payload)
        assert data["slug"] == "t1"

    def test_get_matching_for_query_entries(self, tmp_path):
        cache = self._make_cache(tmp_path)
        cache.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                           "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        cache.store_entry("t1", {"id": "e1", "content": "c",
                                  "author": "a", "role": "c",
                                  "timestamp": "2025-01-01T00:01:00+00:00"})
        results = cache.get_matching_for_query(
            "hermes/blackboard/entries/t1/**", "hermes/blackboard"
        )
        assert len(results) == 1
        key, payload = results[0]
        assert "t1" in key
        data = json.loads(payload)
        assert data["id"] == "e1"

    def test_get_matching_wildcard_all(self, tmp_path):
        cache = self._make_cache(tmp_path)
        cache.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                           "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
        cache.store_entry("t1", {"id": "e1", "content": "c",
                                  "author": "a", "role": "c",
                                  "timestamp": "2025-01-01T00:01:00+00:00"})
        cache.store_metadata("t1", "status",
                              {"value": "active", "updated_by": "x",
                               "timestamp": "2025-01-01T00:02:00+00:00"})
        results = cache.get_matching_for_query(
            "hermes/blackboard/**", "hermes/blackboard"
        )
        keys = [r[0] for r in results]
        assert any("index" in k for k in keys), "Should include index"
        assert any("entries" in k for k in keys), "Should include entries"
        assert any("meta" in k for k in keys), "Should include metadata"


# ---------------------------------------------------------------------------
# ZenohBlackboard tests — mock zenoh
# ---------------------------------------------------------------------------

class TestZenohBlackboard:
    @pytest.fixture
    def mock_zenoh_module(self):
        """Build a minimal mock of the eclipse-zenoh module."""
        zenoh_mock = MagicMock()
        config_mock = MagicMock()
        zenoh_mock.Config.return_value = config_mock

        session_mock = MagicMock()
        zenoh_mock.open.return_value = session_mock

        # Queryable recv() — returns None immediately to stop the loop
        queryable_mock = MagicMock()
        ctx_manager = MagicMock()
        ctx_manager.__enter__ = MagicMock(return_value=None)  # None → break
        ctx_manager.__exit__ = MagicMock(return_value=False)
        queryable_mock.recv.return_value = ctx_manager
        session_mock.declare_queryable.return_value = queryable_mock

        return zenoh_mock, session_mock, queryable_mock

    def test_create_topic_publishes_index_message(self, tmp_path, mock_zenoh_module):
        zenoh_mock, session_mock, _ = mock_zenoh_module
        with patch.dict(sys.modules, {"zenoh": zenoh_mock}):
            from plugins.zenoh_blackboard.cache import BlackboardCache
            from plugins.zenoh_blackboard.blackboard import ZenohBlackboard
            cache = BlackboardCache(tmp_path / "bb.db")
            bb = ZenohBlackboard(cache=cache)

            pub_mock = MagicMock()
            session_mock.declare_publisher.return_value = pub_mock

            topic = bb.create_topic("t1", "Test 1", "A test topic", "agent-a")

        assert topic["slug"] == "t1"
        assert topic["topic_name"] == "Test 1"
        # Publisher was declared and put() was called
        session_mock.declare_publisher.assert_called()
        pub_mock.put.assert_called_once()
        raw_payload = pub_mock.put.call_args[0][0]
        data = json.loads(raw_payload)
        assert data["slug"] == "t1"

    def test_write_entry_encodes_json(self, tmp_path, mock_zenoh_module):
        zenoh_mock, session_mock, _ = mock_zenoh_module
        with patch.dict(sys.modules, {"zenoh": zenoh_mock}):
            from plugins.zenoh_blackboard.cache import BlackboardCache
            from plugins.zenoh_blackboard.blackboard import ZenohBlackboard
            cache = BlackboardCache(tmp_path / "bb.db")
            cache.store_topic({"slug": "t1", "topic_name": "T", "description": "",
                               "created_by": "a", "created_at": "2025-01-01T00:00:00+00:00"})
            bb = ZenohBlackboard(cache=cache)

            pub_mock = MagicMock()
            session_mock.declare_publisher.return_value = pub_mock

            entry = bb.write_entry("t1", "Hello world", "agent-a")

        assert entry["content"] == "Hello world"
        pub_mock.put.assert_called_once()
        raw_payload = pub_mock.put.call_args[0][0]
        data = json.loads(raw_payload)
        assert data["content"] == "Hello world"
        assert data["author"] == "agent-a"

    def test_metadata_update_uses_correct_key_expr(self, tmp_path, mock_zenoh_module):
        zenoh_mock, session_mock, _ = mock_zenoh_module
        with patch.dict(sys.modules, {"zenoh": zenoh_mock}):
            from plugins.zenoh_blackboard.cache import BlackboardCache
            from plugins.zenoh_blackboard.blackboard import ZenohBlackboard
            cache = BlackboardCache(tmp_path / "bb.db")
            bb = ZenohBlackboard(namespace="hermes/blackboard", cache=cache)

            pub_mock = MagicMock()
            session_mock.declare_publisher.return_value = pub_mock

            bb.update_metadata("t1", "status", "active", "agent-a")

        # Publisher declared with the meta key path
        declared_key = session_mock.declare_publisher.call_args[0][0]
        assert "meta" in declared_key
        assert "t1" in declared_key
        assert "status" in declared_key

    def test_query_history_deduplicates_by_id(self, tmp_path, mock_zenoh_module):
        zenoh_mock, session_mock, _ = mock_zenoh_module
        entry = {"id": "e1", "content": "c", "author": "a",
                 "role": "r", "timestamp": "2025-01-01T00:00:00+00:00"}
        # Two replies with same entry_id (two peers responded)
        replies = [
            _make_reply_ok("hermes/blackboard/entries/t1/e1", json.dumps(entry).encode()),
            _make_reply_ok("hermes/blackboard/entries/t1/e1", json.dumps(entry).encode()),
        ]
        session_mock.get.return_value = iter(replies)

        with patch.dict(sys.modules, {"zenoh": zenoh_mock}):
            from plugins.zenoh_blackboard.blackboard import ZenohBlackboard
            bb = ZenohBlackboard()

            results = bb.query_history("hermes/blackboard/entries/t1/**", timeout=1.0)

        assert len(results) == 1  # deduplicated


# ---------------------------------------------------------------------------
# Watcher tests
# ---------------------------------------------------------------------------

class TestBlackboardWatcher:
    def test_notification_queued_on_subscriber_callback(self):
        from plugins.zenoh_blackboard.watcher import BlackboardWatcher, Notification

        watcher = BlackboardWatcher()
        watcher._topic_names["research"] = "AI Research"
        watcher.register_session("sess-1")

        callback = watcher._make_callback("sess-1", "research")
        entry = {"id": "e1", "content": "Finding", "author": "agent-b",
                 "role": "contributor", "timestamp": "2025-01-01T00:00:00+00:00"}
        sample = _make_sample("hermes/blackboard/entries/research/e1", json.dumps(entry).encode())
        callback(sample)

        notifications = watcher.poll("sess-1", limit=10)
        assert len(notifications) == 1
        n = notifications[0]
        assert isinstance(n, Notification)
        assert n.topic_slug == "research"
        assert n.entry["content"] == "Finding"

    def test_poll_returns_empty_before_any_notifications(self):
        from plugins.zenoh_blackboard.watcher import BlackboardWatcher
        watcher = BlackboardWatcher()
        watcher.register_session("sess-2")
        assert watcher.poll("sess-2") == []

    def test_format_notifications(self):
        from plugins.zenoh_blackboard.watcher import BlackboardWatcher, Notification
        watcher = BlackboardWatcher()
        notif = Notification(
            topic_slug="research",
            topic_name="AI Research",
            entry={"content": "Big finding", "author": "agent-b"},
        )
        output = watcher.format_notifications([notif])
        assert "AI Research" in output
        assert "agent-b" in output
        assert "Big finding" in output

    def test_deregister_session_clears_queue(self):
        from plugins.zenoh_blackboard.watcher import BlackboardWatcher
        watcher = BlackboardWatcher()
        watcher.register_session("sess-3")
        watcher.deregister_session("sess-3")
        # After deregister, poll returns [] and doesn't raise
        result = watcher.poll("sess-3")
        assert result == []


# ---------------------------------------------------------------------------
# __init__.py handler tests (no network)
# ---------------------------------------------------------------------------

class TestToolHandlers:
    @pytest.fixture(autouse=True)
    def _patch_zenoh_and_singletons(self, tmp_path, monkeypatch):
        """Reset plugin-level singletons before each test."""
        import plugins.zenoh_blackboard as bb_plugin
        monkeypatch.setattr(bb_plugin, "_blackboard", None)
        monkeypatch.setattr(bb_plugin, "_cache", None)
        monkeypatch.setattr(bb_plugin, "_watcher", None)
        monkeypatch.setattr(bb_plugin, "_plugin_config", {
            "cache_db_path": str(tmp_path / "test.db"),
            "mode": "peer",
        })

        # Patch HERMES_HOME
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

        # Build zenoh mock that keeps the session alive
        zenoh_mock = MagicMock()
        session_mock = MagicMock()
        zenoh_mock.open.return_value = session_mock
        zenoh_mock.Config.return_value = MagicMock()

        queryable_mock = MagicMock()
        ctx_m = MagicMock()
        ctx_m.__enter__ = MagicMock(return_value=None)
        ctx_m.__exit__ = MagicMock(return_value=False)
        queryable_mock.recv.return_value = ctx_m
        session_mock.declare_queryable.return_value = queryable_mock

        pub_mock = MagicMock()
        session_mock.declare_publisher.return_value = pub_mock
        session_mock.get.return_value = iter([])  # no peer replies by default

        monkeypatch.setitem(sys.modules, "zenoh", zenoh_mock)
        self._zenoh = zenoh_mock
        self._session = session_mock
        self._pub = pub_mock

    def test_create_topic(self):
        import plugins.zenoh_blackboard as bb_plugin
        result = json.loads(
            bb_plugin._handle_create_topic(
                {"slug": "research", "name": "Research", "description": "test"},
                agent_identity="agent-a"
            )
        )
        assert result.get("success") is True
        assert result["topic"]["slug"] == "research"

    def test_create_topic_duplicate_rejected(self):
        import plugins.zenoh_blackboard as bb_plugin
        bb_plugin._handle_create_topic({"slug": "dup"}, agent_identity="a")
        result = json.loads(
            bb_plugin._handle_create_topic({"slug": "dup"}, agent_identity="a")
        )
        assert "error" in result
        assert "already exists" in result["error"]

    def test_create_topic_invalid_slug_rejected(self):
        import plugins.zenoh_blackboard as bb_plugin
        result = json.loads(
            bb_plugin._handle_create_topic({"slug": "bad/slug!"}, agent_identity="a")
        )
        assert "error" in result

    def test_write_entry_requires_existing_topic(self):
        import plugins.zenoh_blackboard as bb_plugin
        bb_plugin._ensure_initialized(bb_plugin._plugin_config)
        result = json.loads(
            bb_plugin._handle_write_entry(
                {"slug": "nonexistent", "content": "hello"}, agent_identity="a"
            )
        )
        assert "error" in result

    def test_write_entry_success(self):
        import plugins.zenoh_blackboard as bb_plugin
        bb_plugin._handle_create_topic({"slug": "t1"}, agent_identity="a")
        result = json.loads(
            bb_plugin._handle_write_entry(
                {"slug": "t1", "content": "hello world"}, agent_identity="agent-a"
            )
        )
        assert result.get("success") is True
        assert result["entry"]["content"] == "hello world"

    def test_update_metadata_invalid_key_rejected(self):
        import plugins.zenoh_blackboard as bb_plugin
        bb_plugin._handle_create_topic({"slug": "t1"}, agent_identity="a")
        result = json.loads(
            bb_plugin._handle_update_metadata(
                {"slug": "t1", "key": "bad key!", "value": "x"}, agent_identity="a"
            )
        )
        assert "error" in result

    def test_list_topics_returns_created_topics(self):
        import plugins.zenoh_blackboard as bb_plugin
        bb_plugin._handle_create_topic({"slug": "t1", "name": "Topic One"}, agent_identity="a")
        bb_plugin._handle_create_topic({"slug": "t2", "name": "Topic Two"}, agent_identity="a")
        result = json.loads(bb_plugin._handle_list_topics({}))
        assert result["count"] == 2
        slugs = [t["slug"] for t in result["topics"]]
        assert "t1" in slugs
        assert "t2" in slugs

    def test_watch_and_poll_notifications(self):
        import plugins.zenoh_blackboard as bb_plugin
        from plugins.zenoh_blackboard.watcher import Notification
        bb_plugin._handle_create_topic({"slug": "t1"}, agent_identity="a")

        # Watch
        res = json.loads(
            bb_plugin._handle_watch_topic({"slug": "t1"}, task_id="sess-1")
        )
        assert res.get("success") is True

        # Manually push a notification
        notif = Notification("t1", "T1", {"content": "hi", "author": "agent-b"})
        bb_plugin._watcher._queues["sess-1"].put(notif)

        # Poll
        poll_res = json.loads(
            bb_plugin._handle_poll_notifications({}, task_id="sess-1")
        )
        assert poll_res["count"] == 1
        assert "hi" in poll_res["summary"]
