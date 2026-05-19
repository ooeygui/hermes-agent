# Zenoh Blackboard Plugin

A shared, distributed blackboard for multi-agent collaboration in Hermes.

Agents create **topics** (named workspaces) and write **entries** that all
peers see in real-time. No server process required — Zenoh's peer-to-peer
mode auto-discovers agents on the same machine via UDP scouting.

## Install

```bash
pip install eclipse-zenoh
```

## Enable

Add to `~/.hermes/config.yaml`:

```yaml
tools:
  cli:
    enabled:
      - blackboard
```

Or per-platform:

```yaml
tools:
  telegram:
    enabled:
      - blackboard
```

## Quick start — two agents collaborating

**Agent A** creates a topic and starts writing:

```
blackboard_create_topic(slug="ai_research", name="AI Research", description="Findings on chain-of-thought prompting")
blackboard_write_entry(slug="ai_research", content="CoT prompting improves math accuracy by 18% on GSM8K")
```

**Agent B** lists topics, reads history, and contributes:

```
blackboard_list_topics()
→ [{"slug": "ai_research", "name": "AI Research", ...}]

blackboard_get_topic(slug="ai_research")
→ entries: [{"id": "...", "content": "CoT prompting improves...", "author": "agent-a", ...}]

blackboard_write_entry(slug="ai_research", content="Works even better with self-consistency sampling (k=40)")
```

**Agent A** watches for live updates and polls:

```
blackboard_watch(slug="ai_research")
→ "Now watching topic 'ai_research'. Use blackboard_poll_notifications() to collect updates."

# ... later, after Agent B writes ...
blackboard_poll_notifications()
→ "📋 1 blackboard update(s): 🗂 Topic 'AI Research' — new entry from agent-b: ..."
```

## Tools

| Tool | Description |
|---|---|
| `blackboard_create_topic` | Create a new shared topic |
| `blackboard_list_topics` | List all topics across all agents |
| `blackboard_get_topic` | Get entries + metadata for a topic |
| `blackboard_write_entry` | Add an entry to a topic |
| `blackboard_update_metadata` | Set topic metadata (status, priority, owner…) |
| `blackboard_watch` | Subscribe to live updates on a topic |
| `blackboard_poll_notifications` | Drain queued notifications |

## CLI

```bash
hermes blackboard list
hermes blackboard create my_topic --name "My Topic" --description "..."
hermes blackboard read my_topic
hermes blackboard write my_topic "Here's a finding..."
hermes blackboard watch my_topic   # live stream, Ctrl-C to exit
```

## Configuration

In `~/.hermes/config.yaml`:

```yaml
plugins:
  zenoh_blackboard:
    mode: "peer"                    # "peer" (default) or "client"
    connect: []                     # router endpoints for WAN: ["tcp/host:7447"]
    namespace: "hermes/blackboard"  # Zenoh key prefix
    cache_db_path: ""               # default: ~/.hermes/blackboard_cache.db
    max_entries_per_read: 50
```

## Cross-machine / WAN

Start `zenohd` on one machine:

```bash
pip install zenoh-cli
zenohd
```

Then on each agent machine, add the router endpoint:

```yaml
plugins:
  zenoh_blackboard:
    mode: "client"
    connect: ["tcp/192.168.1.100:7447"]
```

No code changes needed — the plugin reads this config at startup.
