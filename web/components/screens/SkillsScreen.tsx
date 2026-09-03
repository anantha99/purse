"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiClientError } from "@/lib/api";
import type { SkillDetail, SkillSummary } from "@/lib/types";
import Modal from "@/components/Modal";
import { EmptyState, ErrorBanner, Loading } from "@/components/ui";

export default function SkillsScreen() {
  const [skills, setSkills] = useState<SkillSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [body, setBody] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);

  const loadList = useCallback(async () => {
    setError(null);
    try {
      const res = await api.skills();
      setSkills(res.skills);
      if (!selected && res.skills.length) setSelected(res.skills[0].name);
    } catch (err) {
      setSkills([]);
      setError(err instanceof ApiClientError ? err.message : "Couldn't load skills.");
    }
  }, [selected]);

  useEffect(() => {
    loadList();
  }, [loadList]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let alive = true;
    setDetail(null);
    setNotice(null);
    api
      .skill(selected)
      .then((d) => {
        if (!alive) return;
        setDetail(d);
        setBody(d.body);
        setDirty(false);
      })
      .catch((err) => {
        if (!alive) return;
        setError(err instanceof ApiClientError ? err.message : "Couldn't load skill.");
      });
    return () => {
      alive = false;
    };
  }, [selected]);

  async function save() {
    if (!selected) return;
    setSaving(true);
    setNotice(null);
    setError(null);
    try {
      const res = await api.saveSkill(selected, body);
      setNotice(`Saved as version ${res.version}.`);
      setDirty(false);
      await loadList();
      const d = await api.skill(selected);
      setDetail(d);
    } catch (err) {
      setError(err instanceof ApiClientError ? err.message : "Couldn't save the skill.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <div className="topbar">
        <div>
          <span className="h">Skills</span>{" "}
          <span className="sub">markdown · versioned on save</span>
        </div>
        <button
          type="button"
          className="copy-url"
          style={{ marginLeft: "auto" }}
          onClick={() => setNewOpen(true)}
        >
          ＋&nbsp; <b>new skill</b>
        </button>
      </div>

      <div className="content">
        {error && <ErrorBanner message={error} onRetry={loadList} />}
        <div className="skills-grid">
          <aside className="skills-list">
            {skills === null ? (
              <Loading label="Loading…" />
            ) : skills.length === 0 ? (
              <EmptyState title="No skills yet." hint="Create one to get started." />
            ) : (
              skills.map((s) => (
                <button
                  key={s.name}
                  type="button"
                  className={`skill-item${selected === s.name ? " active" : ""}`}
                  onClick={() => setSelected(s.name)}
                >
                  <span className="mono" style={{ fontSize: 13 }}>
                    {s.name}
                  </span>
                  <span className="mono" style={{ fontSize: 11, color: "var(--faint)" }}>
                    v{s.version}
                  </span>
                </button>
              ))
            )}
          </aside>

          <section className="skills-editor">
            {!selected ? (
              <EmptyState title="Select a skill to edit." />
            ) : detail === null ? (
              <Loading label="Loading skill…" />
            ) : (
              <>
                <div className="editor-head">
                  <div>
                    <span className="mono" style={{ fontSize: 14, color: "var(--text)" }}>
                      {detail.name}
                    </span>{" "}
                    <span className="mono" style={{ fontSize: 11, color: "var(--faint)" }}>
                      current v{detail.version}
                      {detail.versions.length > 1 ? ` · ${detail.versions.length} versions` : ""}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={save}
                    disabled={saving || !dirty}
                  >
                    {saving ? "Saving…" : dirty ? "Save (bump version)" : "Saved"}
                  </button>
                </div>
                {notice && <div className="okbar" style={{ marginBottom: 10 }}>{notice}</div>}
                <textarea
                  className="ta"
                  style={{ minHeight: 360, fontSize: 13 }}
                  value={body}
                  onChange={(e) => {
                    setBody(e.target.value);
                    setDirty(true);
                  }}
                  aria-label={`Markdown body for ${detail.name}`}
                  spellCheck={false}
                />
              </>
            )}
          </section>
        </div>
      </div>

      {newOpen && (
        <NewSkillModal
          onClose={() => setNewOpen(false)}
          onCreated={(name) => {
            setNewOpen(false);
            setSelected(name);
            loadList();
          }}
        />
      )}
    </>
  );
}

function NewSkillModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (name: string) => void;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function create() {
    const clean = name.trim();
    if (!/^[a-z0-9][a-z0-9-]*$/.test(clean)) {
      setErr("Use a lowercase slug, e.g. release-checklist.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.saveSkill(clean, `# ${clean}\n\nDescribe this skill in Markdown.\n`);
      onCreated(clean);
    } catch (e) {
      setErr(e instanceof ApiClientError ? e.message : "Couldn't create the skill.");
      setBusy(false);
    }
  }

  return (
    <Modal title="New skill" sub="Names are lowercase slugs. Saving creates version 1." onClose={onClose}>
      {err && <div className="errbar" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      <div className="field">
        <label htmlFor="s-name">Name</label>
        <input
          id="s-name"
          className="ti"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="release-checklist"
          autoFocus
        />
      </div>
      <div className="modal-actions">
        <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={create} disabled={busy}>
          {busy ? "Creating…" : "Create"}
        </button>
      </div>
    </Modal>
  );
}
