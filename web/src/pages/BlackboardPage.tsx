import { useCallback, useEffect, useRef, useState } from "react";
import { BookOpen, RefreshCw, Search } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { api } from "@/lib/api";
import type { BlackboardEntry, BlackboardTopic, BlackboardTopicDetail } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useI18n } from "@/i18n";

const AUTO_REFRESH_MS = 5000;

function formatTime(iso?: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function EntryCard({ entry }: { entry: BlackboardEntry }) {
  return (
    <div className="border-b last:border-b-0 py-3 px-1">
      <div className="flex items-center gap-2 mb-1">
        <span className="text-xs font-medium text-foreground/80">{entry.author}</span>
        {entry.role && entry.role !== "contributor" && (
          <Badge variant="secondary" className="text-xs px-1 py-0">
            {entry.role}
          </Badge>
        )}
        <span className="text-xs text-muted-foreground ml-auto">{formatTime(entry.timestamp)}</span>
      </div>
      <p className="text-sm whitespace-pre-wrap break-words">{entry.content}</p>
    </div>
  );
}

function TopicRow({
  topic,
  selected,
  onClick,
}: {
  topic: BlackboardTopic;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-3 py-2 rounded-md transition-colors hover:bg-accent/60 ${
        selected ? "bg-accent text-accent-foreground font-medium" : ""
      }`}
    >
      <div className="text-sm font-medium truncate">{topic.name || topic.slug}</div>
      {topic.description && (
        <div className="text-xs text-muted-foreground truncate mt-0.5">{topic.description}</div>
      )}
    </button>
  );
}

export default function BlackboardPage() {
  const { t } = useI18n();
  const bb = t.blackboard;

  const [topics, setTopics] = useState<BlackboardTopic[]>([]);
  const [topicsLoading, setTopicsLoading] = useState(true);
  const [topicsError, setTopicsError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [detail, setDetail] = useState<BlackboardTopicDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [autoRefresh, setAutoRefresh] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [sinceTs, setSinceTs] = useState<string>("");
  const [newEntries, setNewEntries] = useState<BlackboardEntry[]>([]);
  const entriesEndRef = useRef<HTMLDivElement>(null);

  const loadTopics = useCallback(async () => {
    try {
      const res = await api.getBlackboardTopics(search, 200);
      setTopics(res.topics);
      setTopicsError(null);
    } catch (e: unknown) {
      setTopicsError(e instanceof Error ? e.message : String(e));
    } finally {
      setTopicsLoading(false);
    }
  }, [search]);

  useEffect(() => {
    setTopicsLoading(true);
    loadTopics();
  }, [loadTopics]);

  // When selected topic changes, load full detail
  useEffect(() => {
    if (!selectedSlug) {
      setDetail(null);
      setSinceTs("");
      setNewEntries([]);
      return;
    }
    setDetailLoading(true);
    setDetailError(null);
    setNewEntries([]);
    api
      .getBlackboardTopic(selectedSlug, 50)
      .then((d) => {
        setDetail(d);
        // Track the last entry timestamp as the cursor for incremental polling
        const ts = d.entries.length > 0 ? d.entries[d.entries.length - 1].timestamp : "";
        setSinceTs(ts);
        setLastUpdated(new Date());
      })
      .catch((e: unknown) => {
        setDetailError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => setDetailLoading(false));
  }, [selectedSlug]);

  // Auto-refresh: poll for new entries since last timestamp
  useEffect(() => {
    if (!autoRefresh || !selectedSlug) return;
    const timer = setInterval(async () => {
      try {
        const res = await api.getBlackboardEntries(selectedSlug, 50, sinceTs);
        if (res.entries.length > 0) {
          setNewEntries((prev) => [...prev, ...res.entries]);
          setSinceTs(res.entries[res.entries.length - 1].timestamp);
          setLastUpdated(new Date());
          // scroll to bottom
          setTimeout(() => entriesEndRef.current?.scrollIntoView({ behavior: "smooth" }), 100);
        }
      } catch {
        // silently ignore poll errors
      }
    }, AUTO_REFRESH_MS);
    return () => clearInterval(timer);
  }, [autoRefresh, selectedSlug, sinceTs]);

  const allEntries = detail ? [...detail.entries, ...newEntries] : [];
  const filteredTopics = topics; // server-side search already applied

  const handleManualRefresh = useCallback(async () => {
    await loadTopics();
    if (selectedSlug) {
      setDetailLoading(true);
      try {
        const d = await api.getBlackboardTopic(selectedSlug, 50);
        setDetail(d);
        setNewEntries([]);
        const ts = d.entries.length > 0 ? d.entries[d.entries.length - 1].timestamp : "";
        setSinceTs(ts);
        setLastUpdated(new Date());
      } catch (e: unknown) {
        setDetailError(e instanceof Error ? e.message : String(e));
      } finally {
        setDetailLoading(false);
      }
    }
  }, [loadTopics, selectedSlug]);

  return (
    <div className="flex flex-col gap-4 p-4 sm:p-6 h-full">
      {/* Header */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2">
          <BookOpen className="h-5 w-5 text-muted-foreground" />
          <H2>{bb.title}</H2>
        </div>
        <div className="flex items-center gap-2">
          {lastUpdated && (
            <span className="text-xs text-muted-foreground hidden sm:inline">
              {bb.lastUpdated}: {lastUpdated.toLocaleTimeString()}
            </span>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setAutoRefresh((v) => !v)}
            title={bb.autoRefresh}
          >
            <RefreshCw
              className={`h-4 w-4 ${autoRefresh ? "text-green-500 animate-spin [animation-duration:3s]" : "text-muted-foreground"}`}
            />
            <span className="ml-1 text-xs">{autoRefresh ? bb.autoRefresh : bb.autoRefresh}</span>
          </Button>
          <Button variant="outline" size="sm" onClick={handleManualRefresh}>
            <RefreshCw className="h-4 w-4 mr-1" />
            {bb.refresh}
          </Button>
        </div>
      </div>

      {/* Two-pane layout */}
      <div className="flex gap-4 flex-1 min-h-0 overflow-hidden">
        {/* Left pane: topic list */}
        <div className="w-64 shrink-0 flex flex-col gap-2">
          <div className="relative">
            <Search className="absolute left-2 top-2.5 h-4 w-4 text-muted-foreground pointer-events-none" />
            <Input
              placeholder={bb.searchPlaceholder}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-8 text-sm"
            />
          </div>
          <div className="text-xs text-muted-foreground px-1">
            {bb.topicCount.replace("{count}", String(filteredTopics.length))}
          </div>
          <Card className="flex-1 overflow-auto">
            <CardContent className="p-2 space-y-0.5">
              {topicsLoading && (
                <div className="flex items-center justify-center py-8">
                  <Spinner size="sm" />
                  <span className="ml-2 text-sm text-muted-foreground">{bb.loadingTopics}</span>
                </div>
              )}
              {topicsError && (
                <div className="py-4 text-center text-sm text-destructive">{topicsError}</div>
              )}
              {!topicsLoading && !topicsError && filteredTopics.length === 0 && (
                <div className="py-4 text-center text-sm text-muted-foreground">
                  {search ? bb.noTopicsMatch : bb.noTopics}
                </div>
              )}
              {filteredTopics.map((topic) => (
                <TopicRow
                  key={topic.slug}
                  topic={topic}
                  selected={selectedSlug === topic.slug}
                  onClick={() => setSelectedSlug(topic.slug)}
                />
              ))}
            </CardContent>
          </Card>
        </div>

        {/* Right pane: topic detail + entries */}
        <div className="flex-1 min-w-0 flex flex-col gap-3 overflow-hidden">
          {!selectedSlug && (
            <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
              {bb.selectTopic}
            </div>
          )}

          {selectedSlug && (
            <>
              {/* Topic header */}
              {detail && (
                <Card>
                  <CardHeader className="pb-2 pt-3 px-4">
                    <CardTitle className="text-base">{detail.name || detail.slug}</CardTitle>
                  </CardHeader>
                  <CardContent className="pt-0 pb-3 px-4 space-y-1">
                    {detail.description && (
                      <p className="text-sm text-muted-foreground">{detail.description}</p>
                    )}
                    <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
                      <span>
                        <span className="font-medium">{bb.createdBy}:</span> {detail.created_by}
                      </span>
                      <span>
                        <span className="font-medium">{bb.createdAt}:</span>{" "}
                        {formatTime(detail.created_at)}
                      </span>
                    </div>
                    {Object.keys(detail.metadata ?? {}).length > 0 && (
                      <div className="mt-1">
                        <span className="text-xs font-medium">{bb.metadata}: </span>
                        {Object.entries(detail.metadata).map(([k, v]) => (
                          <Badge key={k} variant="secondary" className="mr-1 text-xs">
                            {k}={String(v)}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </CardContent>
                </Card>
              )}

              {/* Entries feed */}
              <Card className="flex-1 overflow-auto">
                <CardHeader className="pb-1 pt-3 px-4">
                  <CardTitle className="text-sm font-medium">
                    {bb.entries} ({allEntries.length})
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-4 pb-4">
                  {detailLoading && (
                    <div className="flex items-center py-6">
                      <Spinner size="sm" />
                      <span className="ml-2 text-sm text-muted-foreground">
                        {bb.loadingEntries}
                      </span>
                    </div>
                  )}
                  {detailError && (
                    <div className="py-4 text-sm text-destructive">{detailError}</div>
                  )}
                  {!detailLoading && !detailError && allEntries.length === 0 && (
                    <div className="py-6 text-center text-sm text-muted-foreground">
                      {bb.noEntries}
                    </div>
                  )}
                  {allEntries.map((entry) => (
                    <EntryCard key={entry.id} entry={entry} />
                  ))}
                  <div ref={entriesEndRef} />
                </CardContent>
              </Card>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
