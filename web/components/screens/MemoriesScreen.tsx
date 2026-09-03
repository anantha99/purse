"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiClientError } from "@/lib/api";
import type { Memory, MemoryVersion } from "@/lib/types";
import { relativeTime, initiatedLabel } from "@/lib/format";
import CopyMcp from "@/components/CopyMcp";
import Modal from "@/components/Modal";
import { EmptyState, ErrorBanner, Loading } from "@/components/ui";

const KINDS = ["preference", "decision", "fact", "note"];

export default function MemoriesScreen({ mcpUrl }: { mcpUrl: string }) {
  const [items, setItems] = useState<Memory[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);

  const [addOpen, setAddOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Memory | null>(null);
  const [historyTarget, setHistoryTarget] = useState<Memory | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setItems(null);
    try {
      const page = await api.memories({ limit: 50 });
      setItems(page.items);
    } catch (err) {
      setItems([]);
      setError(err instanceof ApiClientError ? err.message : "Couldn't load memories.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Debounced semantic search.
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    const q = query.trim();
    if (!q) {
      setSearching(false);
      return;
    }
    setSearching(true);
    timer.current = setTimeout(async () => {
      try {
        const res = await api.searchMemories(q, 50);
        setItems(res.results);
        setError(null);
      } catch (err) {
        setError(err instanceof ApiClientError ? err.message : "Search failed.");
      } finally {
        setSearching(false);
      }
    }, 300);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [query]);

  function refresh() {
    setQuery("");
    load();
  }

  return (
    <>
      <div className="topbar">
        <div>
          <span className="h">Memories</span>{" "}
          <span className="sub">append-only · current view</span>
        </div>
        <div className="search">
          <span aria-hidden="true">⌕</span>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="search by meaning…"
            aria-label="Search memories by meaning"
          />
        </div>
        <button
          type="button"
          className="copy-url"
          onClick={() => setAddOpen(true)}
        >
          ＋&nbsp; <b>add memory</b>
        </button>
        <CopyMcp url={mcpUrl} />
      </div>

      <div className="content">
        {error && <ErrorBanner message={error} onRetry={refresh} />}
        {items === null ? (
          <Loading label={searching ? "Searching…" : "Loading memories…"} />
        ) : items.length === 0 ? (
          <EmptyState
            title={query ? "No memories match that search." : "No memories yet."}
            hint={
              query
                ? "Try different words — search is by meaning."
                : "Agents write here through the MCP URL, or add one yourself."
            }
          />
        ) : (
          items.map((m) => (
            <MemoryRow
              key={m.id}
              memory={m}
              onEdit={() => setEditTarget(m)}
              onDelete={refresh}
              onHistory={() => setHistoryTarget(m)}
              setError={setError}
            />
          ))
        )}
      </div>

      {addOpen && (
        <AddMemoryModal
          onClose={() => setAddOpen(false)}
          onSaved={() => {
            setAddOpen(false);
            refresh();
          }}
        />
      )}
      {editTarget && (
        <EditMemoryModal
          memory={editTarget}
          onClose={() => setEditTarget(null)}
          onSaved={() => {
            setEditTarget(null);
            refresh();
          }}
        />
      )}
      {historyTarget && (
        <HistoryModal
          memory={historyTarget}
          onClose={() => setHistoryTarget(null)}
        />
      )}
    </>
  );
}

function MemoryRow({
  memory,
  onEdit,
  onDelete,
  onHistory,
  setError,
}: {
  memory: Memory;
  onEdit: () => void;
  onDelete: () => void;
  onHistory: () => void;
  setError: (m: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const client = memory.provenance.client_name ?? "unknown";

  async function del() {
    if (!confirm("Tombstone this memory? It stays in history but leaves the current view.")) {
      return;
    }
    setBusy(true);
    try {
      await api.deleteMemory(memory.id);
      onDelete();
    } catch (err) {
      setError(err instanceof ApiClientError ? err.message : "Couldn't delete.");
      setBusy(false);
    }
  }

  return (
    <div className="mrow">
      <div className="body">{memory.content}</div>
      <div className="meta">
        <span className="pill kind">{memory.kind}</span>
        <span className="pill">
          <span className="k">from</span> {client}
        </span>
        <span className="prov">
          <span className="who">
            {initiatedLabel(memory.provenance.initiated_by)}
          </span>
          <span>{relativeTime(memory.created_at)}</span>
        </span>
      </div>
      <div className="meta">
        {memory.superseded_count > 0 ? (
          <button type="button" className="hist" onClick={onHistory}>
            supersedes an earlier version · {memory.superseded_count} in history
          </button>
        ) : (
          <span />
        )}
        <div className="rowactions">
          <button type="button" className="linkbtn" onClick={onEdit} disabled={busy}>
            edit
          </button>
          <button
            type="button"
            className="linkbtn danger"
            onClick={del}
            disabled={busy}
          >
            {busy ? "…" : "delete"}
          </button>
        </div>
      </div>
    </div>
  );
}

function AddMemoryModal({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: () => void;
}) {
  const [content, setContent] = useState("");
  const [kind, setKind] = useState(KINDS[0]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    if (!content.trim()) {
      setErr("Add some content first.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.addMemory({ content: content.trim(), kind, initiated_by: "user" });
      onSaved();
    } catch (e) {
      setErr(e instanceof ApiClientError ? e.message : "Couldn't save.");
      setBusy(false);
    }
  }

  return (
    <Modal title="Add a memory" sub="Written as user-initiated from the dashboard." onClose={onClose}>
      {err && <div className="errbar" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      <div className="field">
        <label htmlFor="m-kind">Kind</label>
        <select
          id="m-kind"
          className="ti"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        >
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="m-content">Content</label>
        <textarea
          id="m-content"
          className="ta"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder="Stored verbatim — the canonical text."
          autoFocus
        />
      </div>
      <div className="modal-actions">
        <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={save} disabled={busy}>
          {busy ? "Saving…" : "Add memory"}
        </button>
      </div>
    </Modal>
  );
}

function EditMemoryModal({
  memory,
  onClose,
  onSaved,
}: {
  memory: Memory;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [content, setContent] = useState(memory.content);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    if (!content.trim()) {
      setErr("Content can't be empty.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.editMemory(memory.id, content.trim());
      onSaved();
    } catch (e) {
      setErr(e instanceof ApiClientError ? e.message : "Couldn't save.");
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Edit memory"
      sub="Editing supersedes — the old version stays in history."
      onClose={onClose}
    >
      {err && <div className="errbar" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      <div className="field">
        <label htmlFor="e-content">Content</label>
        <textarea
          id="e-content"
          className="ta"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          autoFocus
        />
      </div>
      <div className="modal-actions">
        <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={save} disabled={busy}>
          {busy ? "Saving…" : "Supersede"}
        </button>
      </div>
    </Modal>
  );
}

function HistoryModal({
  memory,
  onClose,
}: {
  memory: Memory;
  onClose: () => void;
}) {
  const [versions, setVersions] = useState<MemoryVersion[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .memoryHistory(memory.id)
      .then((h) => alive && setVersions(h.versions))
      .catch((e) => {
        if (!alive) return;
        setVersions([]);
        setErr(e instanceof ApiClientError ? e.message : "Couldn't load history.");
      });
    return () => {
      alive = false;
    };
  }, [memory.id]);

  return (
    <Modal title="History" sub="Oldest to newest — the supersession chain." onClose={onClose}>
      {err && <div className="errbar" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      {versions === null ? (
        <Loading label="Loading history…" />
      ) : versions.length === 0 ? (
        <EmptyState title="No prior versions." />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {versions.map((v) => (
            <div key={v.id} className="panel">
              <div style={{ color: "var(--text)", fontSize: 14 }}>{v.content}</div>
              <div
                className="mono"
                style={{ color: "var(--faint)", fontSize: 11, marginTop: 8, display: "flex", gap: 12 }}
              >
                <span>{relativeTime(v.created_at)}</span>
                <span>{initiatedLabel(v.provenance.initiated_by)}</span>
                {v.provenance.client_name && <span>from {v.provenance.client_name}</span>}
                {v.tombstoned && <span style={{ color: "var(--danger)" }}>tombstoned</span>}
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="modal-actions">
        <button type="button" className="btn btn-ghost" onClick={onClose}>
          Close
        </button>
      </div>
    </Modal>
  );
}
