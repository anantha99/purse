"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiClientError } from "@/lib/api";
import type { AuditEntry } from "@/lib/types";
import { EmptyState, ErrorBanner, Loading } from "@/components/ui";
import { relativeTime } from "@/lib/format";

export default function AuditScreen() {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setEntries(null);
    try {
      const res = await api.audit(100);
      setEntries(res.entries);
    } catch (err) {
      setEntries([]);
      setError(err instanceof ApiClientError ? err.message : "Couldn't load the audit log.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <>
      <div className="topbar">
        <div>
          <span className="h">Audit</span>{" "}
          <span className="sub">last 100 · newest first</span>
        </div>
      </div>
      <div className="content">
        {error && <ErrorBanner message={error} onRetry={load} />}
        {entries === null ? (
          <Loading label="Loading audit log…" />
        ) : entries.length === 0 ? (
          <EmptyState title="No audit entries yet." hint="Writes and revocations show up here." />
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table className="dtable">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Action</th>
                  <th>Target</th>
                  <th>Client</th>
                  <th>Agent</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e, i) => (
                  <tr key={`${e.created_at}-${i}`}>
                    <td title={e.created_at}>{relativeTime(e.created_at)}</td>
                    <td className="strong">{e.action}</td>
                    <td>
                      {e.target_type}
                      <span style={{ color: "var(--faint)" }}> · {e.target_id}</span>
                    </td>
                    <td>{e.client_name ?? "—"}</td>
                    <td>{e.agent_id ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
