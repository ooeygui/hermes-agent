"""Blackboard plugin — register(ctx) entry point and tool handlers.

The 7 tools work identically regardless of backend (local or zenoh).
Select backend via config:
    plugins:
      blackboard:
        backend: "local"   # default — SQLite only, no extra deps
        backend: "zenoh"   # distributed — requires: pip install eclipse-zenoh
"""

from __future__ import annotations

import json
import logging
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


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _ok(**kwargs) -> str:
    return json.dumps({"success": True, **kwargs})


# ---------------------------------------------------------------------------
# Plugin-level singletons
# ---------------------------------------------------------------------------

_core: Optional[Any] = None   # BlackboardCore
_watcher: Optional[Any] = None  # BlackboardWatcher
_backend_instance: Optional[Any] = None
_namespace: str = "hermes/blackboard"
_plugin_config: dict = {}


def _ensure_initialized() -> bool:
    global _core, _watcher, _backend_instance, _namespace

    if _core is not None:
        return True

    cfg = _plugin_config
    _namespace = cfg.get("namespace", "hermes/blackboard")
    backend_name = cfg.get("backend", "local").strip().lower()

    from hermes_constants import get_hermes_home
    db_path_str = cfg.get("cache_db_path", "")
    db_path = Path(db_path_str) if db_path_str else get_hermes_home() / "blackboard_cache.db"

    from plugins.blackboard.cache import BlackboardCache
    from plugins.blackboard.watcher import BlackboardWatcher
    from plugins.blackboard.core import BlackboardCore

    cache = BlackboardCache(db_path)

    if backend_name == "zenoh":
        from plugins.blackboard.backends.zenoh import ZenohBackend
        if not ZenohBackend.is_available():
            logger.warning(
                "blackboard: backend=zenoh requested but eclipse-zenoh is not installed. "
                "Falling back to local. Run: pip install eclipse-zenoh"
            )
            from plugins.blackboard.backend import LocalBackend
            backend = LocalBackend()
        else:
            backend = ZenohBackend(
                mode=cfg.get("zenoh", {}).get("mode", "peer"),
                connect_endpoints=cfg.get("zenoh", {}).get("connect", []),
                cache=cache,
            )
    else:
        from plugins.blackboard.backend import LocalBackend
        backend = LocalBackend()

    backend.open(_namespace)
    _backend_instance = backend
    _core = BlackboardCore(cache=cache, backend=backend, namespace=_namespace)
    _watcher = BlackboardWatcher()

    logger.info("blackboard plugin initialised (backend=%s, db=%s)", backend_name, db_path)
    return True


def _cache():
    return _core._cache if _core else None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _handle_create_topic(args: dict, **kw) -> str:
    if not _ensure_initialized():
        return _err("Blackboard failed to initialise.")
    slug = args.get("slug", "").strip().lower()
    name = args.get("name", "").strip() or slug
    description = args.get("description", "").strip()
    if not slug:
        return _err("'slug' is required.")
    if not _valid_slug(slug):
        return _err(
            f"Invalid slug '{slug}'. Use letters/digits/dots/hyphens/underscores, "
            f"1–64 chars, starting with a letter or digit."
        )
    if _cache() and _cache().get_topic(slug):
        return _err(f"Topic '{slug}' already exists.")
    author = kw.get("agent_identity", "unknown")
    topic = _core.create_topic(slug, name, description, author)
    return _ok(topic=topic, message=f"Topic '{slug}' created.")


def _handle_list_topics(args: dict, **kw) -> str:
    if not _ensure_initialized():
        return _err("Blackboard failed to initialise.")
    timeout = float(args.get("timeout", 1.5))
    topics = _core.list_topics(timeout=timeout)
    return _ok(topics=topics, count=len(topics))


def _handle_get_topic(args: dict, **kw) -> str:
    if not _ensure_initialized():
        return _err("Blackboard failed to initialise.")
    slug = args.get("slug", "").strip()
    if not slug:
        return _err("'slug' is required.")
    limit = int(args.get("limit", _plugin_config.get("max_entries_per_read", 50)))
    result = _core.get_topic(
        slug,
        since=args.get("since"),
        limit=limit,
        timeout=float(args.get("timeout", 2.0)),
    )
    if not result:
        return _err(
            f"Topic '{slug}' not found. "
            "Use blackboard_list_topics() to see available topics."
        )
    return _ok(**result)


def _handle_write_entry(args: dict, **kw) -> str:
    if not _ensure_initialized():
        return _err("Blackboard failed to initialise.")
    slug = args.get("slug", "").strip()
    content = args.get("content", "").strip()
    role = args.get("role", "contributor").strip()
    if not slug:
        return _err("'slug' is required.")
    if not content:
        return _err("'content' is required.")
    if not (_cache() and _cache().get_topic(slug)):
        return _err(
            f"Topic '{slug}' not found. "
            "Create it first with blackboard_create_topic()."
        )
    author = kw.get("agent_identity", "unknown")
    entry = _core.write_entry(slug, content, author, role)
    return _ok(entry=entry, message="Entry written.")


def _handle_update_metadata(args: dict, **kw) -> str:
    if not _ensure_initialized():
        return _err("Blackboard failed to initialise.")
    slug = args.get("slug", "").strip()
    key = args.get("key", "").strip()
    value = args.get("value")
    if not slug:
        return _err("'slug' is required.")
    if not key:
        return _err("'key' is required.")
    if not _valid_key(key):
        return _err(
            f"Invalid key '{key}'. Use letters/digits/dots/hyphens/underscores only."
        )
    if value is None:
        return _err("'value' is required.")
    author = kw.get("agent_identity", "unknown")
    result = _core.update_metadata(slug, key, value, author)
    return _ok(metadata_key=key, updated=result)


def _handle_watch(args: dict, **kw) -> str:
    if not _ensure_initialized():
        return _err("Blackboard failed to initialise.")
    slug = args.get("slug", "").strip()
    if not slug:
        return _err("'slug' is required.")
    topic = _cache().get_topic(slug) if _cache() else None
    if not topic:
        return _err(
            f"Topic '{slug}' not found. "
            "Create it first or use blackboard_list_topics()."
        )
    session_id = kw.get("task_id") or kw.get("session_id") or "default"
    created = _watcher.watch_topic(
        session_id=session_id,
        slug=slug,
        topic_name=topic.get("name", slug),
        backend=_backend_instance,
        namespace=_namespace,
    )
    msg = (
        f"Now watching topic '{slug}'. "
        "Use blackboard_poll_notifications() to collect updates."
        if created
        else f"Already watching topic '{slug}'."
    )
    return _ok(message=msg)


def _handle_poll(args: dict, **kw) -> str:
    if not _ensure_initialized():
        return _err("Blackboard failed to initialise.")
    limit = int(args.get("limit", 10))
    session_id = kw.get("task_id") or kw.get("session_id") or "default"
    _watcher.register_session(session_id)
    notifications = _watcher.poll(session_id=session_id, limit=limit)
    return json.dumps({
        "success": True,
        "count": len(notifications),
        "summary": _watcher.format_notifications(notifications),
        "notifications": [
            {"topic_slug": n.topic_slug, "topic_name": n.topic_name, "entry": n.entry}
            for n in notifications
        ],
    })


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_SCHEMAS = [
    {
        "name": "blackboard_create_topic",
        "description": (
            "Create a new shared blackboard topic that all agents can read and contribute to. "
            "Topics are named workspaces (e.g. 'ai_research', 'sprint_24'). "
            "Once created, write entries with blackboard_write_entry()."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": (
                        "URL-safe unique identifier. Letters, digits, dots, hyphens, underscores; "
                        "1–64 chars; must start with a letter or digit."
                    ),
                },
                "name": {"type": "string", "description": "Human-readable display name. Defaults to slug."},
                "description": {"type": "string", "description": "What this topic is about."},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "blackboard_list_topics",
        "description": (
            "List all blackboard topics. With a distributed backend, queries peers for the "
            "latest index and merges with local storage."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timeout": {"type": "number", "description": "Seconds to wait for peer responses (distributed only). Default: 1.5."},
            },
            "required": [],
        },
    },
    {
        "name": "blackboard_get_topic",
        "description": (
            "Read all entries and metadata for a topic. With a distributed backend, "
            "queries peers for fresh entries. Use 'since' for incremental reads."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Topic slug."},
                "since": {"type": "string", "description": "ISO 8601 timestamp — return only entries after this time."},
                "limit": {"type": "integer", "description": "Max entries to return. Default: 50."},
                "timeout": {"type": "number", "description": "Seconds to wait for peer history (distributed only). Default: 2.0."},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "blackboard_write_entry",
        "description": (
            "Write a new entry to a topic. Published to all subscribers in real-time "
            "(in-process for local backend; across peers for distributed backends). "
            "Always stored durably in local SQLite."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Topic slug."},
                "content": {"type": "string", "description": "The content — a finding, question, summary, etc."},
                "role": {"type": "string", "description": "Your role: 'contributor', 'coordinator', 'reviewer', etc.", "default": "contributor"},
            },
            "required": ["slug", "content"],
        },
    },
    {
        "name": "blackboard_update_metadata",
        "description": "Set or update a metadata key-value pair on a topic (status, owner, priority, tags, etc.).",
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Topic slug."},
                "key": {"type": "string", "description": "Metadata key. Letters, digits, dots, hyphens, underscores."},
                "value": {"description": "Value to store (string, number, boolean, or object)."},
            },
            "required": ["slug", "key", "value"],
        },
    },
    {
        "name": "blackboard_watch",
        "description": (
            "Subscribe to live updates on a topic. New entries from any agent (or human) "
            "will be queued as notifications. Follow up with blackboard_poll_notifications()."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Topic slug to watch."},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "blackboard_poll_notifications",
        "description": (
            "Retrieve pending notifications from watched topics. "
            "Returns entries that arrived since the last poll."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max notifications to return. Default: 10."},
            },
            "required": [],
        },
    },
]

_HANDLERS = {
    "blackboard_create_topic": _handle_create_topic,
    "blackboard_list_topics": _handle_list_topics,
    "blackboard_get_topic": _handle_get_topic,
    "blackboard_write_entry": _handle_write_entry,
    "blackboard_update_metadata": _handle_update_metadata,
    "blackboard_watch": _handle_watch,
    "blackboard_poll_notifications": _handle_poll,
}


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------

def _setup_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="bb_cmd", help="blackboard commands")

    subs.add_parser("list", help="List all topics").set_defaults(func=_cli_list)

    rp = subs.add_parser("read", help="Read entries for a topic")
    rp.add_argument("slug"); rp.add_argument("--limit", type=int, default=50)
    rp.set_defaults(func=_cli_read)

    wp = subs.add_parser("write", help="Write an entry")
    wp.add_argument("slug"); wp.add_argument("content")
    wp.set_defaults(func=_cli_write)

    cp = subs.add_parser("create", help="Create a topic")
    cp.add_argument("slug")
    cp.add_argument("--name", default="")
    cp.add_argument("--description", default="")
    cp.set_defaults(func=_cli_create)

    wtp = subs.add_parser("watch", help="Live-watch a topic (Ctrl-C to exit)")
    wtp.add_argument("slug")
    wtp.set_defaults(func=_cli_watch)


def _cli_list(args) -> None:
    _ensure_initialized()
    result = json.loads(_handle_list_topics({}))
    topics = result.get("topics", [])
    if not topics:
        print("No topics found.")
        return
    for t in topics:
        print(f"  {t['slug']:<32} {t.get('name', '')}")


def _cli_read(args) -> None:
    _ensure_initialized()
    result = json.loads(_handle_get_topic({"slug": args.slug, "limit": args.limit}))
    if "error" in result:
        print(f"Error: {result['error']}"); return
    topic = result.get("topic", {}); entries = result.get("entries", [])
    print(f"Topic: {topic.get('name', args.slug)} [{args.slug}]")
    print(f"  {topic.get('description', '')}")
    print(f"  {len(entries)} entries:")
    for e in entries:
        ts = e.get("timestamp", "")[:19]
        print(f"  [{ts}] {e.get('author', '?')} ({e.get('role', '?')}): {e.get('content', '')}")


def _cli_write(args) -> None:
    _ensure_initialized()
    result = json.loads(_handle_write_entry(
        {"slug": args.slug, "content": args.content}, agent_identity="cli-user"
    ))
    print(f"Error: {result['error']}" if "error" in result else f"Written: {result['entry']['id']}")


def _cli_create(args) -> None:
    _ensure_initialized()
    result = json.loads(_handle_create_topic(
        {"slug": args.slug, "name": args.name, "description": args.description},
        agent_identity="cli-user",
    ))
    print(f"Error: {result['error']}" if "error" in result else f"Created: {result['topic']['slug']}")


def _cli_watch(args) -> None:
    import time as _time
    _ensure_initialized()
    if not _backend_instance:
        print("Blackboard not initialised."); return

    print(f"Watching '{args.slug}' (Ctrl-C to stop)...")
    seen: set = set()

    def _on_sample(sample) -> None:
        try:
            entry = json.loads(sample.payload.to_bytes())
        except Exception:
            return
        eid = entry.get("id", "")
        if eid not in seen:
            seen.add(eid)
            ts = entry.get("timestamp", "")[:19]
            print(f"[{ts}] {entry.get('author', '?')}: {entry.get('content', '')}")

    key_pattern = f"{_namespace}/entries/{args.slug}/**"
    handle = _backend_instance.subscribe(key_pattern, _on_sample)
    try:
        while True:
            _time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        _backend_instance.unsubscribe(handle)


# ---------------------------------------------------------------------------
# Session lifecycle hooks
# ---------------------------------------------------------------------------

def _on_session_end(session_id: str = None, **kw) -> None:
    if _watcher and session_id:
        _watcher.deregister_session(session_id, backend=_backend_instance)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    global _plugin_config

    try:
        from hermes_cli.config import load_config
        from hermes_cli.config_utils import cfg_get
        _plugin_config = cfg_get(load_config(), "plugins", "blackboard", default={}) or {}
    except Exception:
        _plugin_config = {}

    for schema in _SCHEMAS:
        name = schema["name"]
        ctx.register_tool(
            name=name,
            toolset="blackboard",
            schema=schema,
            handler=lambda args, _n=name, **kw: _HANDLERS[_n](args, **kw),
            description=schema["description"],
            emoji="🗂",
        )

    ctx.register_hook("on_session_end", _on_session_end)

    ctx.register_cli_command(
        name="blackboard",
        help="Manage the shared agent blackboard",
        setup_fn=_setup_cli,
        description="Read, write, watch, and manage shared blackboard topics.",
    )

    logger.info("blackboard plugin registered (%d tools)", len(_SCHEMAS))
