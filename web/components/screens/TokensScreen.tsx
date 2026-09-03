"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiClientError } from "@/lib/api";
import type { Connection, MintTokenResponse } from "@/lib/types";
import Modal from "@/components/Modal";
import CopyButton from "@/components/CopyButton";
import { EmptyState, ErrorBanner, Loading } from "@/components/ui";
import { relativeTime } from "@/lib/format";

const ALL_SCOPES = [
  "memory:read",
  "memory:write",
  "skills:read",
  "skills:write",
  "apis:use",
];

export default function TokensScreen() {
  const [rows, setRows] = useState<Connection[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mintOpen, setMintOpen] = useState(false);
  const [minted, setMinted] = useState<MintTokenResponse | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setRows(null);
    try {
      const res = await api.connections();
      setRows(res.connections);
    } catch (err) {
      setRows([]);
      setError(err instanceof ApiClientError ? err.message : "Couldn't load tokens.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function revoke(id: string) {
    if (!confirm("Revoke this token? Any client using it loses access.")) return;
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
          <span className="h">Tokens</span>{" "}
          <span className="sub">personal access tokens · shown once</span>
        </div>
        <button
          type="button"
          className="copy-url"
          style={{ marginLeft: "auto" }}
          onClick={() => setMintOpen(true)}
        >
          ＋&nbsp; <b>mint token</b>
        </button>
      </div>

      <div className="content">
        {error && <ErrorBanner message={error} onRetry={load} />}
        {rows === null ? (
          <Loading label="Loading tokens…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No tokens yet."
            hint="Mint a personal access token to connect an agent that can't do OAuth."
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
              return (
                <div className="r" key={c.id}>
                  <div className="client">
                    <span className={`st${revoked ? " off" : ""}`} aria-hidden="true" />
                    <div>
                      <b>{c.client_name}</b>{" "}
                      <span className="id">· {revoked ? "revoked" : "active"}</span>
                    </div>
                  </div>
                  <div className="amode">{c.auth_mode}</div>
                  <div className="scopes">
                    {c.scopes.map((s) => (
                      <span key={s} className={`scope${s.endsWith(":write") ? " w" : ""}`}>
                        {s}
                      </span>
                    ))}
                    {c.writes_enabled && <span className="badge-writes">writes on</span>}
                  </div>
                  <div>
                    {revoked ? (
                      <span className="mono" style={{ fontSize: 11, color: "var(--faint)" }}>
                        {c.revoked_at ? relativeTime(c.revoked_at) : "revoked"}
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

      {mintOpen && (
        <MintModal
          onClose={() => setMintOpen(false)}
          onMinted={(res) => {
            setMintOpen(false);
            setMinted(res);
            load();
          }}
        />
      )}
      {minted && <TokenShownModal minted={minted} onClose={() => setMinted(null)} />}
    </>
  );
}

function MintModal({
  onClose,
  onMinted,
}: {
  onClose: () => void;
  onMinted: (res: MintTokenResponse) => void;
}) {
  const [clientName, setClientName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["memory:read"]);
  const [writes, setWrites] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function toggle(scope: string) {
    setScopes((prev) =>
      prev.includes(scope) ? prev.filter((s) => s !== scope) : [...prev, scope],
    );
  }

  async function mint() {
    if (!clientName.trim()) {
      setErr("Give the client a name.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const res = await api.mintToken({
        client_name: clientName.trim(),
        scopes,
        writes_enabled: writes,
      });
      onMinted(res);
    } catch (e) {
      setErr(e instanceof ApiClientError ? e.message : "Couldn't mint the token.");
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Mint a personal access token"
      sub="You'll see the token once. Copy it before closing."
      onClose={onClose}
    >
      {err && <div className="errbar" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      <div className="field">
        <label htmlFor="t-name">Client name</label>
        <input
          id="t-name"
          className="ti"
          value={clientName}
          onChange={(e) => setClientName(e.target.value)}
          placeholder="e.g. codex"
          autoFocus
        />
      </div>
      <div className="field">
        <label>Scopes</label>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {ALL_SCOPES.map((s) => (
            <label
              key={s}
              className={`scope${scopes.includes(s) ? " w" : ""}`}
              style={{ cursor: "pointer", display: "inline-flex", gap: 6, alignItems: "center" }}
            >
              <input
                type="checkbox"
                checked={scopes.includes(s)}
                onChange={() => toggle(s)}
                style={{ accentColor: "var(--accent)" }}
              />
              {s}
            </label>
          ))}
        </div>
      </div>
      <div className="field">
        <label
          style={{ display: "inline-flex", gap: 8, alignItems: "center", textTransform: "none", letterSpacing: 0 }}
        >
          <input
            type="checkbox"
            checked={writes}
            onChange={(e) => setWrites(e.target.checked)}
            style={{ accentColor: "var(--accent)" }}
          />
          Enable writes
        </label>
      </div>
      <div className="modal-actions">
        <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={mint} disabled={busy}>
          {busy ? "Minting…" : "Mint token"}
        </button>
      </div>
    </Modal>
  );
}

function TokenShownModal({
  minted,
  onClose,
}: {
  minted: MintTokenResponse;
  onClose: () => void;
}) {
  return (
    <Modal
      title="Token minted"
      sub="This is the only time the token is shown. Store it somewhere safe."
      onClose={onClose}
    >
      <div className="token-show">{minted.token}</div>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
        <CopyButton value={minted.token} className="linkbtn" label="copy token" copiedLabel="copied ✓" />
      </div>
      <div className="okbar">
        Connection <b className="mono">{minted.connection.client_name}</b> created with{" "}
        {minted.connection.scopes.join(", ")}
        {minted.connection.writes_enabled ? " · writes on" : ""}.
      </div>
      <div className="modal-actions">
        <button type="button" className="btn btn-primary" onClick={onClose}>
          I&apos;ve copied it
        </button>
      </div>
    </Modal>
  );
}
