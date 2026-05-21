import { Box, Text, useInput, useStdout } from '@hermes/ink'
import { useEffect, useRef, useState } from 'react'

import type { GatewayClient } from '../gatewayClient.js'
import { rpcErrorMessage } from '../lib/rpc.js'
import type { Theme } from '../theme.js'
import { OverlayHint, useOverlayKeys, windowItems, windowOffset } from './overlayControls.js'

// ── Types ────────────────────────────────────────────────────────────

interface BlackboardTopic {
  slug: string
  name: string
  description?: string
  created_by?: string
  created_at?: string
}

interface BlackboardEntry {
  id: string
  slug: string
  content: string
  author?: string
  role?: string
  timestamp?: string
}

interface BlackboardOverlayProps {
  gw: GatewayClient
  onClose: () => void
  t: Theme
}

// ── Constants ────────────────────────────────────────────────────────

const TOPIC_VISIBLE = 14
const ENTRY_VISIBLE = 18
const REFRESH_MS = 5000
const MAX_ENTRY_WIDTH = 90

// ── Helpers ──────────────────────────────────────────────────────────

function fmtTime(iso?: string): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleTimeString()
  } catch {
    return iso.slice(0, 19)
  }
}

function truncate(s: string, max: number): string {
  return s.length <= max ? s : s.slice(0, max - 1) + '…'
}

// ── Blackboard overlay ───────────────────────────────────────────────

export function BlackboardOverlay({ gw, onClose, t }: BlackboardOverlayProps) {
  const { stdout } = useStdout()
  const cols = stdout?.columns ?? 80
  const rows = stdout?.rows ?? 24

  // Topic list state
  const [topics, setTopics] = useState<BlackboardTopic[]>([])
  const [topicsLoading, setTopicsLoading] = useState(true)
  const [topicsErr, setTopicsErr] = useState('')
  const [topicIdx, setTopicIdx] = useState(0)
  const [search, setSearch] = useState('')
  const [searchMode, setSearchMode] = useState(false)

  // Entry pane state
  const [entries, setEntries] = useState<BlackboardEntry[]>([])
  const [detail, setDetail] = useState<BlackboardTopic | null>(null)
  const [entriesLoading, setEntriesLoading] = useState(false)
  const [entriesErr, setEntriesErr] = useState('')
  const [sinceTs, setSinceTs] = useState('')
  const [entryOffset, setEntryOffset] = useState(0)
  const [focus, setFocus] = useState<'topics' | 'entries'>('topics')

  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // ── Load topics ──────────────────────────────────────────────────

    const loadTopics = () => {
    gw.request<{ topics?: BlackboardTopic[]; total?: number }>('blackboard.list', {
      search,
      limit: 200
    })
      .then(r => {
        const newTopics = r?.topics ?? []
        setTopics(newTopics)
        setTopicIdx(i => Math.min(i, Math.max(0, newTopics.length - 1)))
        setTopicsErr('')
        setTopicsLoading(false)
      })
      .catch((e: unknown) => {
        setTopicsErr(rpcErrorMessage(e))
        setTopicsLoading(false)
      })
  }

  useEffect(() => {
    setTopicsLoading(true)
    loadTopics()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search])

  // ── Load entries for selected topic ─────────────────────────────

  const selectedTopic = topics[topicIdx]

  const loadEntries = (topic: BlackboardTopic) => {
    setEntriesLoading(true)
    setEntriesErr('')
    gw.request<{ entries?: BlackboardEntry[]; slug?: string; name?: string; metadata?: unknown }>('blackboard.get_topic', {
      slug: topic.slug,
      limit: 50
    })
      .then(r => {
        const ents = r?.entries ?? []
        setEntries(ents)
        setDetail({ ...topic, ...(r ?? {}) })
        const last = ents[ents.length - 1]?.timestamp ?? ''
        setSinceTs(last)
        setEntryOffset(0)
        setEntriesLoading(false)
      })
      .catch((e: unknown) => {
        setEntriesErr(rpcErrorMessage(e))
        setEntriesLoading(false)
      })
  }

  useEffect(() => {
    if (!selectedTopic) {
      setEntries([])
      setDetail(null)
      return
    }
    loadEntries(selectedTopic)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTopic?.slug])

  // ── Auto-refresh entries ─────────────────────────────────────────

  useEffect(() => {
    if (!selectedTopic) return

    const poll = () => {
      gw.request<{ entries?: BlackboardEntry[] }>('blackboard.get_topic', {
        slug: selectedTopic.slug,
        since: sinceTs,
        limit: 50
      })
        .then(r => {
          const newEnts = r?.entries ?? []
          if (newEnts.length > 0) {
            setEntries(prev => [...prev, ...newEnts])
            setSinceTs(newEnts[newEnts.length - 1]?.timestamp ?? sinceTs)
          }
        })
        .catch(() => { /* silently ignore poll errors */ })
        .finally(() => {
          refreshTimerRef.current = setTimeout(poll, REFRESH_MS)
        })
    }

    refreshTimerRef.current = setTimeout(poll, REFRESH_MS)
    return () => {
      if (refreshTimerRef.current != null) clearTimeout(refreshTimerRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTopic?.slug, sinceTs])

  // ── Input handling ───────────────────────────────────────────────

  useOverlayKeys({
    onClose,
    onBack: focus === 'entries' ? () => setFocus('topics') : undefined,
    disabled: searchMode
  })

  useInput((ch, key) => {
    // Search mode input
    if (searchMode) {
      if (key.escape || key.return) {
        setSearchMode(false)
        return
      }
      if (key.backspace || key.delete) {
        setSearch(s => s.slice(0, -1))
        return
      }
      if (ch && ch.length === 1 && !key.ctrl && !key.meta) {
        setSearch(s => s + ch)
      }
      return
    }

    // Normal nav
    if (ch === '/') {
      setSearchMode(true)
      return
    }

    if (ch === 'r') {
      loadTopics()
      if (selectedTopic) loadEntries(selectedTopic)
      return
    }

    if (focus === 'topics') {
      if (key.upArrow || ch === 'k') {
        setTopicIdx(i => Math.max(0, i - 1))
      } else if (key.downArrow || ch === 'j') {
        setTopicIdx(i => Math.min(topics.length - 1, i + 1))
      } else if (key.return || ch === 'l') {
        if (selectedTopic) setFocus('entries')
      }
    } else {
      if (key.upArrow || ch === 'k') {
        setEntryOffset(o => Math.max(0, o - 1))
      } else if (key.downArrow || ch === 'j') {
        setEntryOffset(o => Math.min(Math.max(0, entries.length - ENTRY_VISIBLE), o + 1))
      } else if (key.left || ch === 'h') {
        setFocus('topics')
      }
    }
  })

  // ── Layout ───────────────────────────────────────────────────────

  const listWidth = Math.min(32, Math.floor(cols * 0.3))
  const detailWidth = cols - listWidth - 3
  const visibleTopicCount = Math.max(4, rows - 6)

  const filteredTopics = topics

  const topicWindow = windowItems(filteredTopics, topicIdx, visibleTopicCount)
  const entryWindow = entries.slice(entryOffset, entryOffset + ENTRY_VISIBLE)
  const entryMaxWidth = Math.min(detailWidth - 2, MAX_ENTRY_WIDTH)

  // ── Render ───────────────────────────────────────────────────────

  return (
    <Box flexDirection="column" flexGrow={1}>
      {/* Title bar */}
      <Box>
        <Text bold color={t.color.primary}> 🗂  Blackboard</Text>
        {searchMode && (
          <Text color={t.color.accent}> /{search}<Text color={t.color.muted}>_</Text></Text>
        )}
        {!searchMode && search && (
          <Text color={t.color.muted}> filter: {search}</Text>
        )}
        <Text color={t.color.muted}> ({filteredTopics.length} topic{filteredTopics.length !== 1 ? 's' : ''})</Text>
      </Box>

      {/* Main two-pane area */}
      <Box flexGrow={1} flexDirection="row">
        {/* ── Left: topic list ── */}
        <Box flexDirection="column" width={listWidth} borderStyle="single" borderColor={focus === 'topics' ? t.color.accent : t.color.border}>
          {topicsLoading && <Text color={t.color.muted}> loading…</Text>}
          {topicsErr && <Text color={t.color.error}> {truncate(topicsErr, listWidth - 2)}</Text>}
          {!topicsLoading && !topicsErr && filteredTopics.length === 0 && (
            <Text color={t.color.muted}> no topics</Text>
          )}
          {topicWindow.items.map((topic, i) => {
            const absIdx = topicWindow.offset + i
            const isSelected = absIdx === topicIdx
            return (
              <Box key={topic.slug}>
                <Text
                  color={isSelected ? t.color.primary : t.color.text}
                  bold={isSelected}
                  wrap="truncate-end"
                >
                  {isSelected ? '▶ ' : '  '}
                  {truncate(topic.name || topic.slug, listWidth - 4)}
                </Text>
              </Box>
            )
          })}
        </Box>

        {/* ── Right: entries pane ── */}
        <Box flexDirection="column" flexGrow={1} borderStyle="single" borderColor={focus === 'entries' ? t.color.accent : t.color.border}>
          {!selectedTopic && (
            <Box flexGrow={1} alignItems="center" justifyContent="center">
              <Text color={t.color.muted}>Select a topic with ↵ or l</Text>
            </Box>
          )}

          {selectedTopic && (
            <>
              {/* Topic header */}
              <Box paddingX={1}>
                <Text bold color={t.color.primary} wrap="truncate-end">
                  {truncate(detail?.name || selectedTopic.name || selectedTopic.slug, entryMaxWidth)}
                </Text>
                {detail?.created_by && (
                  <Text color={t.color.muted}>  by {detail.created_by}</Text>
                )}
              </Box>
              {detail?.description && (
                <Box paddingX={1}>
                  <Text color={t.color.muted} wrap="truncate-end">
                    {truncate(detail.description, entryMaxWidth)}
                  </Text>
                </Box>
              )}

              {/* Divider */}
              <Text color={t.color.border}>{'─'.repeat(Math.min(entryMaxWidth + 2, cols))}</Text>

              {/* Entries */}
              {entriesLoading && <Text color={t.color.muted}> loading entries…</Text>}
              {entriesErr && <Text color={t.color.error}> {entriesErr}</Text>}
              {!entriesLoading && !entriesErr && entries.length === 0 && (
                <Text color={t.color.muted}> no entries yet</Text>
              )}
              {entryWindow.map((entry) => (
                <Box key={entry.id} flexDirection="column" paddingX={1} marginBottom={1}>
                  <Box>
                    <Text color={t.color.accent}>{entry.author ?? 'unknown'}</Text>
                    {entry.role && entry.role !== 'contributor' && (
                      <Text color={t.color.muted}> [{entry.role}]</Text>
                    )}
                    <Text color={t.color.muted}> {fmtTime(entry.timestamp)}</Text>
                  </Box>
                  <Text wrap="wrap" color={t.color.text}>
                    {truncate(entry.content, entryMaxWidth * 3)}
                  </Text>
                </Box>
              ))}
              {entries.length > ENTRY_VISIBLE && (
                <Text color={t.color.muted}>
                  {' '}showing {entryOffset + 1}–{Math.min(entryOffset + ENTRY_VISIBLE, entries.length)} of {entries.length}
                </Text>
              )}
            </>
          )}
        </Box>
      </Box>

      {/* Footer hint */}
      <OverlayHint t={t}>
        {searchMode
          ? 'type to filter — esc/enter to exit search'
          : focus === 'topics'
            ? 'j/k navigate  ↵/l open  / search  r refresh  q close'
            : 'j/k scroll  h/esc back  r refresh  q close'
        }
      </OverlayHint>
    </Box>
  )
}
