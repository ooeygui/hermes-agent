"""Zenoh Blackboard plugin — register(ctx) entry point and tool handlers."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_KEY_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def _valid_slug(s: str) -> bool:
    return bool(_SLUG_RE.match(s))


def _valid_key(k: str) -> bool:
    return bool(_KEY_RE.match(k))


def _tool_error(msg: str) -> str:
    return json.dumps({"error": msg})


def _tool_ok(**kwargs) -> str:
    return json.dumps({"success": True, **kwargs})


# ---------------------------------------------------------------------------
# Plugin-level singletons (created lazily on first use)
# ---------------------------------------------------------------------------

_blackboard: Optional[Any] = None  # ZenohBlackboard
_cache: Optional[Any] = None  # BlackboardCache
_watcher: Optional[Any] = None  # BlackboardWatcher
_namespace: str = "hermes/blackboard"


def _is_available() -> bool:
    try:
        import zenoh  # noqa: F401
        return True
    except ImportError:
        return False


def _unavailable_error() -> str:
    return _tool_error(
        "eclipse-zenoh is not installed. "
        "Run: pip install eclipse-zenoh  (then restart Hermes)"
    )


def _ensure_initialized(config: dict = None) -> bool:
    """Lazily create the cache and blackboard singletons."""
    global _blackboard, _cache, _watcher, _namespace

    if _blackboard is not None:
        return True

    if not _is_available():
        return False

    cfg = config or {}
    _namespace = cfg.get("namespace", "hermes/blackboard")
    mode = cfg.get("mode", "peer")
    connect_endpoints = cfg.get("connect", [])

    from hermes_constants import get_hermes_home

    db_path_str = cfg.get("cache_db_path", "")
    db_path = Path(db_path_str) if db_path_str else get_hermes_home() / "blackboard_cache.db"

    from plugins.zenoh_blackboard.cache import BlackboardCache
    from plugins.zenoh_blackboard.blackboard import ZenohBlackboard
    from plugins.zenoh_blackboard.watcher import BlackboardWatcher

    _cache = BlackboardCache(db_path)
    _blackboard = ZenohBlackboard(
        namespace=_namespace,
        mode=mode,
        connect_endpoints=connect_endpoints,
        cache=_cache,
    )
    _watcher = BlackboardWatcher()

    logger.info("Zenoh blackboard plugin initialized (mode=%s, db=%s)", mode, db_path)
    return True


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _handle_create_topic(args: dict, **kwargs) -> str:
    if not _ensure_initialized(_plugin_config):
        return _unavailable_error()

    slug = args.get("slug", "").strip().lower()
    name = args.get("name", "").strip()
    description = args.get("description", "").strip()

    if not slug:
        return _tool_error("'slug' is required.")
    if not _valid_slug(slug):
        return _tool_error(
            f"Invalid slug '{slug}'. Use letters, digits, dots, hyphens, underscores only "
            f"(1-64 chars, must start with letter/digit)."
        )
    if not name:
        name = slug

    author = kwargs.get("agent_identity", "unknown-agent")

    # Check topic doesn't already exist
    existing = _cache.get_topic(slug) if _cache else None
    if existing:
        return _tool_error(f"Topic '{slug}' already exists.")

    topic = _blackboard.create_topic(
        slug=slug, name=name, description=description, created_by=author
    )
    return _tool_ok(topic=topic, message=f"Topic '{slug}' created.")


def _handle_list_topics(args: dict, **kwargs) -> str:
    if not _ensure_initialized(_plugin_config):
        return _unavailable_error()

    # Try to pull fresh data from peers via Zenoh get
    try:
        _blackboard.ensure_open()
        fresh = _blackboard.query_history(
            f"{_namespace}/index", timeout=float(args.get("timeout", 1.5))
        )
        for t in fresh:
            if t.get("slug"):
                _cache.store_topic(t)
    except Exception as exc:
        logger.debug("list_topics: peer query failed, using cache only: %s", exc)

    topics = _cache.get_topics() if _cache else []
    return _tool_ok(topics=topics, count=len(topics))


def _handle_get_topic(args: dict, **kwargs) -> str:
    if not _ensure_initialized(_plugin_config):
        return _unavailable_error()

    slug = args.get("slug", "").strip()
    if not slug:
        return _tool_error("'slug' is required.")

    since = args.get("since")
    limit = int(args.get("limit", _plugin_config.get("max_entries_per_read", 50)))

    # Pull fresh entries from peers
    try:
        _blackboard.ensure_open()
        fresh_entries = _blackboard.query_history(
            f"{_namespace}/entries/{slug}/**",
            timeout=float(args.get("timeout", 2.0)),
        )
        for e in fresh_entries:
            if e.get("id"):
                _cache.store_entry(slug, e)
    except Exception as exc:
        logger.debug("get_topic entries: peer query failed: %s", exc)

    topic = _cache.get_topic(slug) if _cache else None
    if not topic:
        return _tool_error(f"Topic '{slug}' not found. Use blackboard_list_topics() to see available topics.")

    entries = _cache.get_entries(slug, since=since, limit=limit)
    metadata = _cache.get_metadata(slug)

    return _tool_ok(
        topic=topic,
        entries=entries,
        entry_count=len(entries),
        metadata=metadata,
    )


def _handle_write_entry(args: dict, **kwargs) -> str:
    if not _ensure_initialized(_plugin_config):
        return _unavailable_error()

    slug = args.get("slug", "").strip()
    content = args.get("content", "").strip()
    role = args.get("role", "contributor").strip()

    if not slug:
        return _tool_error("'slug' is required.")
    if not content:
        return _tool_error("'content' is required.")

    # Ensure the topic exists in cache (may not if we're first to write)
    topic = _cache.get_topic(slug) if _cache else None
    if not topic:
        return _tool_error(
            f"Topic '{slug}' not found. Create it first with blackboard_create_topic()."
        )

    author = kwargs.get("agent_identity", "unknown-agent")
    entry = _blackboard.write_entry(slug=slug, content=content, author=author, role=role)
    return _tool_ok(entry=entry, message="Entry written.")


def _handle_update_metadata(args: dict, **kwargs) -> str:
    if not _ensure_initialized(_plugin_config):
        return _unavailable_error()

    slug = args.get("slug", "").strip()
    key = args.get("key", "").strip()
    value = args.get("value")

    if not slug:
        return _tool_error("'slug' is required.")
    if not key:
        return _tool_error("'key' is required.")
    if not _valid_key(key):
        return _tool_error(
            f"Invalid key '{key}'. Use letters, digits, dots, hyphens, underscores only."
        )
    if value is None:
        return _tool_error("'value' is required.")

    author = kwargs.get("agent_identity", "unknown-agent")
    result = _blackboard.update_metadata(slug=slug, key=key, value=value, updated_by=author)
    return _tool_ok(metadata_key=key, updated=result)


def _handle_watch_topic(args: dict, **kwargs) -> str:
    if not _ensure_initialized(_plugin_config):
        return _unavailable_error()

    slug = args.get("slug", "").strip()
    if not slug:
        return _tool_error("'slug' is required.")

    topic = _cache.get_topic(slug) if _cache else None
    if not topic:
        return _tool_error(
            f"Topic '{slug}' not found. Create it first or use blackboard_list_topics()."
        )

    session_id = kwargs.get("task_id") or kwargs.get("session_id") or "default"

    created = _watcher.watch_topic(
        session_id=session_id,
        slug=slug,
        topic_name=topic.get("name", slug),
        blackboard=_blackboard,
    )

    if created:
        return _tool_ok(
            message=f"Now watching topic '{slug}'. "
                    "Use blackboard_poll_notifications() to collect updates."
        )
    else:
        return _tool_ok(
            message=f"Already watching topic '{slug}'."
        )


def _handle_poll_notifications(args: dict, **kwargs) -> str:
    if not _ensure_initialized(_plugin_config):
        return _unavailable_error()

    limit = int(args.get("limit", 10))
    session_id = kwargs.get("task_id") or kwargs.get("session_id") or "default"

    _watcher.register_session(session_id)
    notifications = _watcher.poll(session_id=session_id, limit=limit)
    formatted = _watcher.format_notifications(notifications)

    return json.dumps({
        "success": True,
        "count": len(notifications),
        "summary": formatted,
        "notifications": [
            {
                "topic_slug": n.topic_slug,
                "topic_name": n.topic_name,
                "entry": n.entry,
            }
            for n in notifications
        ],
    })


# ---------------------------------------------------------------------------
# Plugin config (set during register(), used by handlers)
# ---------------------------------------------------------------------------

_plugin_config: dict = {}


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_SCHEMAS = [
    {
        "name": "blackboard_create_topic",
        "description": (
            "Create a new shared blackboard topic that all agents can read and contribute to. "
            "Topics are named workspaces for a specific area of collaboration (e.g. 'ai_research', "
            "'code_review_42'). Once created, agents write entries with blackboard_write_entry()."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": (
                        "URL-safe unique identifier for the topic. "
                        "Letters, digits, dots, hyphens, underscores; 1-64 chars. "
                        "Example: 'ai_research', 'code-review-42'."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Human-readable display name for the topic. Defaults to slug.",
                },
                "description": {
                    "type": "string",
                    "description": "What this topic is about. Helps other agents understand its purpose.",
                },
            },
            "required": ["slug"],
        },
    },
    {
        "name": "blackboard_list_topics",
        "description": (
            "List all active blackboard topics across all agents. "
            "Queries peers for the latest topic index and merges with local cache. "
            "Returns topic slugs, names, descriptions, creators, and creation times."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timeout": {
                    "type": "number",
                    "description": "Seconds to wait for peer responses. Default: 1.5.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "blackboard_get_topic",
        "description": (
            "Get the full history of entries and metadata for a blackboard topic. "
            "Queries peer agents for their cached entries and merges with local storage. "
            "Use 'since' to request only entries after a specific ISO timestamp."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "The topic slug (from blackboard_list_topics).",
                },
                "since": {
                    "type": "string",
                    "description": "ISO 8601 timestamp — return only entries after this time.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum entries to return. Default: 50.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Seconds to wait for peer history responses. Default: 2.0.",
                },
            },
            "required": ["slug"],
        },
    },
    {
        "name": "blackboard_write_entry",
        "description": (
            "Write a new entry to a blackboard topic. "
            "Entries are published to all subscribed agents in real-time and cached durably. "
            "Use this to share findings, conclusions, questions, or any content with collaborators."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "The topic slug to write to.",
                },
                "content": {
                    "type": "string",
                    "description": "The content to add. Can be a finding, question, summary, etc.",
                },
                "role": {
                    "type": "string",
                    "description": "Your role in this contribution. E.g. 'contributor', 'coordinator', 'reviewer'.",
                    "default": "contributor",
                },
            },
            "required": ["slug", "content"],
        },
    },
    {
        "name": "blackboard_update_metadata",
        "description": (
            "Set or update a metadata key-value pair on a topic. "
            "Use metadata for topic-level state: status, priority, owner, tags, etc. "
            "All agents can read and update metadata; last-write-wins per key."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "The topic slug.",
                },
                "key": {
                    "type": "string",
                    "description": "Metadata key name. Letters, digits, dots, hyphens, underscores.",
                },
                "value": {
                    "description": "Value to store (string, number, boolean, or object).",
                },
            },
            "required": ["slug", "key", "value"],
        },
    },
    {
        "name": "blackboard_watch",
        "description": (
            "Subscribe to live updates on a blackboard topic. "
            "Future entries written by any agent will be queued as notifications. "
            "After calling this, use blackboard_poll_notifications() periodically to receive updates."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "The topic slug to watch.",
                },
            },
            "required": ["slug"],
        },
    },
    {
        "name": "blackboard_poll_notifications",
        "description": (
            "Retrieve queued notifications from watched blackboard topics. "
            "Returns entries that arrived since the last poll. "
            "Call this after calling blackboard_watch() to check for new contributions from other agents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum notifications to return per call. Default: 10.",
                },
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# CLI subcommand setup
# ---------------------------------------------------------------------------

def _setup_cli(subparser) -> None:
    """Register 'hermes blackboard <verb>' subcommands."""
    subs = subparser.add_subparsers(dest="bb_cmd", help="blackboard commands")

    list_p = subs.add_parser("list", help="List all topics")
    list_p.set_defaults(func=_cli_list)

    read_p = subs.add_parser("read", help="Read entries for a topic")
    read_p.add_argument("slug", help="Topic slug")
    read_p.add_argument("--limit", type=int, default=50)
    read_p.set_defaults(func=_cli_read)

    write_p = subs.add_parser("write", help="Write an entry to a topic")
    write_p.add_argument("slug", help="Topic slug")
    write_p.add_argument("content", help="Entry content")
    write_p.set_defaults(func=_cli_write)

    watch_p = subs.add_parser("watch", help="Live-watch a topic (Ctrl-C to exit)")
    watch_p.add_argument("slug", help="Topic slug")
    watch_p.set_defaults(func=_cli_watch)

    create_p = subs.add_parser("create", help="Create a new topic")
    create_p.add_argument("slug", help="Topic slug")
    create_p.add_argument("--name", default="")
    create_p.add_argument("--description", default="")
    create_p.set_defaults(func=_cli_create)


def _cli_list(args) -> None:
    import json as _json
    _ensure_initialized(_plugin_config)
    result = _json.loads(_handle_list_topics({}))
    topics = result.get("topics", [])
    if not topics:
        print("No topics found.")
        return
    for t in topics:
        print(f"  {t['slug']!s:<30} {t.get('name','')}")


def _cli_read(args) -> None:
    import json as _json
    _ensure_initialized(_plugin_config)
    result = _json.loads(_handle_get_topic({"slug": args.slug, "limit": args.limit}))
    if "error" in result:
        print(f"Error: {result['error']}")
        return
    entries = result.get("entries", [])
    topic = result.get("topic", {})
    print(f"Topic: {topic.get('name', args.slug)} [{args.slug}]")
    print(f"  {topic.get('description', '')}")
    print(f"  {len(entries)} entries:")
    for e in entries:
        ts = e.get("timestamp", "")[:19]
        print(f"  [{ts}] {e.get('author','?')}: {e.get('content','')}")


def _cli_write(args) -> None:
    import json as _json
    _ensure_initialized(_plugin_config)
    result = _json.loads(
        _handle_write_entry({"slug": args.slug, "content": args.content},
                             agent_identity="cli-user")
    )
    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(f"Entry written: {result['entry']['id']}")


def _cli_watch(args) -> None:
    import signal, time as _time, json as _json
    _ensure_initialized(_plugin_config)
    if not _is_available():
        print("eclipse-zenoh not installed.")
        return

    print(f"Watching '{args.slug}' (Ctrl-C to stop)...")
    seen: set = set()

    def _on_sample(sample) -> None:
        try:
            entry = _json.loads(sample.payload.to_bytes())
        except Exception:
            return
        eid = entry.get("id", "")
        if eid not in seen:
            seen.add(eid)
            ts = entry.get("timestamp", "")[:19]
            print(f"[{ts}] {entry.get('author','?')}: {entry.get('content','')}")

    _blackboard.ensure_open()
    key_expr = f"{_namespace}/entries/{args.slug}/**"
    sub = _blackboard.subscribe(key_expr, _on_sample)
    try:
        while True:
            _time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        _blackboard.unsubscribe(key_expr)


def _cli_create(args) -> None:
    import json as _json
    _ensure_initialized(_plugin_config)
    result = _json.loads(
        _handle_create_topic(
            {"slug": args.slug, "name": args.name, "description": args.description},
            agent_identity="cli-user",
        )
    )
    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(f"Topic created: {result['topic']['slug']}")


# ---------------------------------------------------------------------------
# Session lifecycle hooks
# ---------------------------------------------------------------------------

def _on_session_end(session_id: str = None, **kwargs) -> None:
    if _watcher and session_id:
        _watcher.deregister_session(session_id, blackboard=_blackboard)


# ---------------------------------------------------------------------------
# Plugin entrypoint
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Called by the Hermes plugin loader to wire in this plugin."""
    global _plugin_config

    # Read plugin config from the host config
    try:
        from hermes_cli.config import load_config
        from hermes_cli.config_utils import cfg_get
        config = load_config()
        _plugin_config = cfg_get(config, "plugins", "zenoh_blackboard", default={}) or {}
    except Exception:
        _plugin_config = {}

    if not _is_available():
        logger.warning(
            "zenoh_blackboard: eclipse-zenoh not installed — tools registered but will "
            "return helpful install instructions. Run: pip install eclipse-zenoh"
        )

    # Register all 7 tools
    for schema in _SCHEMAS:
        handler_map = {
            "blackboard_create_topic": _handle_create_topic,
            "blackboard_list_topics": _handle_list_topics,
            "blackboard_get_topic": _handle_get_topic,
            "blackboard_write_entry": _handle_write_entry,
            "blackboard_update_metadata": _handle_update_metadata,
            "blackboard_watch": _handle_watch_topic,
            "blackboard_poll_notifications": _handle_poll_notifications,
        }
        tool_name = schema["name"]
        ctx.register_tool(
            name=tool_name,
            toolset="blackboard",
            schema=schema,
            handler=lambda args, _tn=tool_name, **kw: handler_map[_tn](args, **kw),
            description=schema["description"],
            emoji="🗂",
        )

    # Session lifecycle: clean up subscriptions on session end
    ctx.register_hook("on_session_end", _on_session_end)

    # CLI subcommand: hermes blackboard <verb>
    ctx.register_cli_command(
        name="blackboard",
        help="Manage the shared agent blackboard",
        setup_fn=_setup_cli,
        description="Read, write, watch, and manage shared blackboard topics across agents.",
    )

    logger.info("zenoh_blackboard plugin registered (%d tools)", len(_SCHEMAS))
