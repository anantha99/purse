"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, ApiClientError } from "@/lib/api";
import type { Connection } from "@/lib/types";
import CopyMcp from "@/components/CopyMcp";
import { EmptyState, ErrorBanner, Loading } from "@/components/ui";
import { relativeTime, formatAuthMode } from "@/lib/format";

export default function ConnectionsScreen({ mcpUrl }: { mcpUrl: string }) {
  const [rows, setRows] = useState<Connection[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setRows(null);
    try {
      const res = await api.connections();
      setRows(res.connections);
    } catch (err) {
      setRows([]);
      setError(err instanceof ApiClientError ? err.message : "Couldn't load connections.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function revoke(id: string) {
    if (!confirm("Revoke this connection? The client can no longer open the vault.")) return;
    try {
      await api.revokeConnection(id);
      load();
    } catch (err) {
      setError(err instanceof ApiClientError ? err.message : "Couldn't revoke.");
    }
  }

  return (
    <>
      <div className="topbar">
        <div>
          <span className="h">Connections</span>{" "}
          <span className="sub">who can open this vault</span>
        </div>
        <Link className="copy-url" href="/dashboard/tokens" style={{ marginLeft: "auto" }}>
          ＋&nbsp; <b>new token</b>
        </Link>
        <CopyMcp url={mcpUrl} />
      </div>

      <div className="content">
        {error && <ErrorBanner message={error} onRetry={load} />}
        {rows === null ? (
          <Loading label="Loading connections…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No connections yet."
            hint="Agents appear here after they authenticate, or mint a token in Tokens."
          />
        ) : (
          <div className="ctable">
            <div className="r head">
              <span>Client</span>
              <span>Auth mode</span>
              <span>Scopes</span>
              <span />
            </div>
            {rows.map((c) => {
              const revoked = Boolean(c.revoked_at);
              const idle =
                !revoked &&
                c.last_seen_at &&
                Date.now() - new Date(c.last_seen_at).getTime() >
                  7 * 24 * 60 * 60 * 1000;
              const online = !revoked && !idle;
              return (
                <div className="r" key={c.id}>
                  <div className="client">
                    <span className={`st${online ? "" : " off"}`} aria-hidden="true" />
                    <div>
                      <b>{c.client_name}</b>{" "}
                      <span className="id">
                        · {revoked ? "revoked" : idle ? "idle" : "active"}
                      </span>
                    </div>
                  </div>
                  <div className="amode">{formatAuthMode(c.auth_mode)}</div>
                  <div className="scopes">
                    {c.scopes.map((s) => (
                      <span
                        key={s}
                        className={`scope${s.endsWith(":write") ? " w" : ""}`}
                      >
                        {s}
                      </span>
                    ))}
                    {c.writes_enabled && <span className="badge-writes">writes on</span>}
                  </div>
                  <div>
                    {revoked ? (
                      <span
                        className="mono"
                        style={{ fontSize: 11, color: "var(--faint)" }}
                        title={c.revoked_at ?? undefined}
                      >
                        revoked {c.revoked_at ? relativeTime(c.revoked_at) : ""}
                      </span>
                    ) : (
                      <button type="button" className="revoke" onClick={() => revoke(c.id)}>
                        revoke
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </>
  );
}
