# Blackboard Plugin

A shared workspace for multi-agent and human–agent collaboration in Hermes.

Works **out of the box with no extra dependencies** — the default local backend
stores everything in SQLite and fires in-process notifications. Add
[Zenoh](#distributed-zenoh-backend) when you need real-time sync across
separate processes or machines.

---

## Table of contents

1. [Install & enable](#install--enable)
2. [Local backend (default)](#local-backend-default)
3. [Distributed: Zenoh backend](#distributed-zenoh-backend)
4. [Configuration reference](#configuration-reference)
5. [Tools reference](#tools-reference)
6. [CLI reference](#cli-reference)
7. [Walkthrough: research team (2 agents + 1 human)](#walkthrough-research-team-2-agents--1-human)
8. [Walkthrough: code-review pipeline](#walkthrough-code-review-pipeline)
9. [How it works](#how-it-works)

---

## Install & enable

No extra packages required for the local backend.

```yaml
# ~/.hermes/config.yaml
tools:
  cli:
    enabled:
      - blackboard
```

For messaging gateways (Telegram, Discord, Slack, …):

```yaml
tools:
  telegram:
    enabled:
      - blackboard
```

Verify the tools are active:

```
/tools
```

You should see seven `blackboard_*` tools listed.

---

## Local backend (default)

The default backend stores all data in a single SQLite file at
`~/.hermes/blackboard_cache.db` (or the active profile's home).

**No extra install.  No server.  No network.**

### What works locally

| Feature | Local |
|---|---|
| Create / list / read topics | ✅ |
| Write entries | ✅ |
| Update metadata | ✅ |
| In-process `blackboard_watch` + `blackboard_poll_notifications` | ✅ |
| Cross-process real-time notifications | ❌ (use Zenoh) |
| Multi-machine sync | ❌ (use Zenoh) |

In-process means: sub-agents spawned by `delegate_task` that run in the
**same Python process** (not a subprocess) will receive notifications.
Sub-agents in separate processes share the SQLite file and can read each
other's entries via `blackboard_get_topic`, but live watch notifications
do not cross process boundaries without Zenoh.

---

## Distributed: Zenoh backend

Switch to the Zenoh backend for real-time pub/sub across processes or machines.

### Install

```bash
pip install eclipse-zenoh
```

### Enable

```yaml
# ~/.hermes/config.yaml
plugins:
  blackboard:
    backend: zenoh
    zenoh:
      mode: peer      # default — auto-discovers peers on localhost via UDP
      connect: []     # add router endpoints for WAN (see below)
```

With `mode: peer`, Zenoh auto-discovers agents on the same machine with no
configuration beyond the above. All agents must use the same `namespace`
(default: `hermes/blackboard`).

### Cross-machine / WAN

Start a Zenoh router on a shared host:

```bash
pip install zenoh-cli
zenohd
```

Then on each agent machine:

```yaml
plugins:
  blackboard:
    backend: zenoh
    zenoh:
      mode: client
      connect: ["tcp/192.168.1.100:7447"]
```

### What the Zenoh backend adds

| Feature | Local | Zenoh |
|---|---|---|
| In-process notifications | ✅ | ✅ |
| Cross-process notifications (same machine) | ❌ | ✅ |
| Multi-machine real-time sync | ❌ | ✅ |
| Catch-up history for late-joining agents | ❌ | ✅ |
| Extra install | none | `pip install eclipse-zenoh` |
| Extra server | none | none (peer) or `zenohd` (WAN) |

---

## Configuration reference

```yaml
# ~/.hermes/config.yaml
plugins:
  blackboard:
    backend: "local"                 # "local" (default) or "zenoh"
    namespace: "hermes/blackboard"   # key prefix — change to isolate teams
    cache_db_path: ""                # default: ~/.hermes/blackboard_cache.db
    max_entries_per_read: 50

    # Zenoh-specific (only used when backend: zenoh)
    zenoh:
      mode: "peer"                   # "peer" or "client"
      connect: []                    # router endpoints for WAN
```

**Team isolation tip:** give each team a unique namespace so their topics
don't mix:

```yaml
plugins:
  blackboard:
    namespace: "acme/team-alpha/blackboard"
```

---

## Tools reference

| Tool | Description |
|---|---|
| `blackboard_create_topic` | Create a new shared workspace for a subject area |
| `blackboard_list_topics` | List all topics (merges local cache + peer query for Zenoh) |
| `blackboard_get_topic` | Read entries + metadata for a topic |
| `blackboard_write_entry` | Publish a new finding, note, or question |
| `blackboard_update_metadata` | Set topic-level state: status, owner, priority, tags |
| `blackboard_watch` | Subscribe to live updates (works in-process; cross-process needs Zenoh) |
| `blackboard_poll_notifications` | Drain pending notifications from watched topics |

**Topic slugs:** letters, digits, dots, hyphens, underscores; 1–64 chars;
start with a letter or digit. Examples: `ai_research`, `sprint_24`, `pr-review-142`.

**Metadata keys:** same character set, no spaces.

---

## CLI reference

```bash
# List all topics
hermes blackboard list

# Create a topic
hermes blackboard create sprint_24 --name "Sprint 24" --description "Current sprint"

# Read entries
hermes blackboard read sprint_24
hermes blackboard read sprint_24 --limit 100

# Write an entry
hermes blackboard write sprint_24 "Auth service is done, ready for review"

# Live-stream new entries (Ctrl-C to stop; Zenoh backend recommended for cross-process)
hermes blackboard watch sprint_24
```

---

## Walkthrough: research team (2 agents + 1 human)

### Step 1 — Human creates the topic

```bash
hermes blackboard create ai_safety \
  --name "AI Safety Research" \
  --description "Collective findings on alignment and interpretability"
```

Or from a Hermes conversation:

```
You: Create a blackboard topic "ai_safety" for our AI safety research.

Hermes: blackboard_create_topic(slug="ai_safety", name="AI Safety Research",
          description="Collective findings on alignment and interpretability")
        ✓ Topic 'ai_safety' created.
```

---

### Step 2 — Researcher agent discovers and contributes

```
[Agent R]
blackboard_list_topics()
→ [{"slug": "ai_safety", "name": "AI Safety Research", ...}]

blackboard_write_entry(
  slug="ai_safety",
  content="Constitutional AI reduces harmful outputs by 34% on ToxiGen (Anthropic 2023). "
          "Key technique: RLHF with human-written principles at SFT stage.",
  role="researcher"
)

blackboard_write_entry(
  slug="ai_safety",
  content="Open question: does constitutional AI scale beyond 100B parameters "
          "without reward hacking?",
  role="researcher"
)
```

---

### Step 3 — Critic agent watches and responds

```
[Agent C]
blackboard_watch(slug="ai_safety")
→ "Now watching 'ai_safety'. Use blackboard_poll_notifications() to collect updates."

# ... later ...
blackboard_poll_notifications()
→ 📋 2 blackboard update(s):
    🗂 Topic "AI Safety Research" — new entry from agent-r:
      'Constitutional AI reduces harmful outputs by 34%...'
    🗂 Topic "AI Safety Research" — new entry from agent-r:
      'Open question: does constitutional AI scale...'

blackboard_write_entry(
  slug="ai_safety",
  content="Counter: Perez et al. (2022) show reward hacking at scale even with "
          "constitutional constraints. The open question is likely 'no'.",
  role="critic"
)

blackboard_update_metadata(slug="ai_safety", key="status", value="in_review")
blackboard_update_metadata(slug="ai_safety", key="priority", value="high")
```

---

### Step 4 — Human checks in from the terminal

```bash
hermes blackboard read ai_safety
```

```
Topic: AI Safety Research [ai_safety]
  Collective findings on alignment and interpretability
  3 entries:

  [2026-05-20T10:01:12] agent-r (researcher):
    Constitutional AI reduces harmful outputs by 34%...

  [2026-05-20T10:03:44] agent-r (researcher):
    Open question: does constitutional AI scale...

  [2026-05-20T10:07:31] agent-c (critic):
    Counter: Perez et al. (2022) show reward hacking at scale...
```

Human adds a note:

```bash
hermes blackboard write ai_safety \
  "Discussed with team — prioritise the scaling question. @agent-r please investigate Perez findings."
```

---

### Step 5 — Agent R picks up the human's note and responds

```
[Agent R]
blackboard_poll_notifications()
→ 📋 2 blackboard update(s):
    🗂 Topic "AI Safety Research" — new entry from agent-c: 'Counter: ...'
    🗂 Topic "AI Safety Research" — new entry from cli-user: 'Discussed with team...'

blackboard_write_entry(
  slug="ai_safety",
  content="Investigating Perez et al. — will run replication with open-source RLHF stack.",
  role="researcher"
)
blackboard_update_metadata(slug="ai_safety", key="owner", value="agent-r")
blackboard_update_metadata(slug="ai_safety", key="status", value="in_progress")
```

---

## Walkthrough: code-review pipeline

A coordinator fans out review to three parallel specialists, each writing
findings independently. The coordinator collects them all at the end.

```
[Coordinator]
# 1. Set up the topic
blackboard_create_topic(
  slug="review_pr_142",
  name="PR #142 Code Review",
  description="Multi-agent review of the auth service refactor"
)
blackboard_update_metadata(slug="review_pr_142", key="status", value="open")
blackboard_watch(slug="review_pr_142")

# 2. Spawn three specialists in parallel
delegate_task(goal="Review PR #142 for security issues. "
                   "Write findings to blackboard topic 'review_pr_142' with role='security'.")
delegate_task(goal="Review PR #142 for performance regressions. "
                   "Write findings to blackboard topic 'review_pr_142' with role='performance'.")
delegate_task(goal="Review PR #142 for test coverage gaps. "
                   "Write findings to blackboard topic 'review_pr_142' with role='testing'.")
```

Each sub-agent calls `blackboard_write_entry` as it finds issues.
The coordinator polls and summarises:

```
[Coordinator — after delegates complete]
blackboard_get_topic(slug="review_pr_142")

blackboard_update_metadata(slug="review_pr_142", key="status", value="complete")
blackboard_write_entry(
  slug="review_pr_142",
  content="Summary: 2 security findings (medium), 1 N+1 query (high), "
          "3 missing test cases. Recommend: block merge.",
  role="coordinator"
)
```

Human checks the result:

```bash
hermes blackboard read review_pr_142 --limit 100
```

---

## How it works

```
Agent A (write)          Agent B (watch + read)        CLI / Human
     │                          │                            │
     │  blackboard_create_topic │                            │
     │──→ SQLite (local)        │                            │
     │──→ LocalBackend.publish_topic()                       │
     │    └─ fires in-process subscribers                    │
     │                          │                            │
     │  blackboard_write_entry  │                            │
     │──→ SQLite (always)       │                            │
     │──→ backend.publish_entry()                            │
     │    LocalBackend: fires in-process callbacks           │
     │    ZenohBackend: pub to Zenoh key space → subscriber fires
     │                          │←── notification queued     │
     │                          │                            │
     │               blackboard_poll_notifications           │
     │                          │ drains queue               │
     │                          │                            │
     │               blackboard_get_topic                    │
     │  ←── query_history() ───→│  ZenohBackend: get from peers
     │    LocalBackend: [] (SQLite is source of truth)       │
     │                          │                            │
     │                          │     hermes blackboard read │
     │                          │←──────────────── CLI read  │
```

**Key design properties:**

- **No dependencies for basic use** — `LocalBackend` is pure Python + stdlib SQLite
- **Same 7 tools, same tool names** regardless of backend — model prompts are backend-agnostic
- **Lazy init** — the backend opens only on first tool call; no overhead for agents that don't use it
- **Durable** — SQLite is written before any network publish; no entry is lost if the network call fails
- **Profile-aware** — cache DB stored under `get_hermes_home()`, not a hardcoded `~/.hermes` path
- **Backend-swappable** — change `backend:` in config; no code changes, no tool changes

### Backend comparison

| | `local` | `zenoh` |
|---|---|---|
| Extra install | none | `pip install eclipse-zenoh` |
| Extra server | none | none (peer) or `zenohd` (WAN) |
| In-process watch | ✅ | ✅ |
| Cross-process watch | ❌ | ✅ |
| Multi-machine sync | ❌ | ✅ |
| History catch-up for late joiners | ❌ | ✅ |
| Best for | Single-machine workflows, testing | Multi-agent, multi-machine collaboration |
