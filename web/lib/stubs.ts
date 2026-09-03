// Built-in stub responses so every screen renders without a live backend.
// Served by the BFF proxy (lib/backend.ts) when PURSE_BACKEND_URL is unset, or
// when a configured backend is unreachable AND PURSE_ALLOW_STUB_FALLBACK=1.
// The real integration path (BFF -> backend) is unchanged by these.

import type {
  AuditResponse,
  ConnectionList,
  MemoryHistory,
  MemoryPage,
  SearchResponse,
  SessionInfo,
  SkillDetail,
  SkillList,
  WorkspaceCounts,
} from "./types";

const now = Date.now();
const iso = (daysAgo: number) =>
  new Date(now - daysAgo * 24 * 60 * 60 * 1000).toISOString();

export const stubSession: SessionInfo = {
  user: { email: "owner@localhost" },
  workspace: { id: "ws_personal", name: "Personal" },
  writes_enabled_default: false,
};

export const stubWorkspace: WorkspaceCounts = {
  memories: 128,
  skills: 3,
  apis: 0,
  connections: 4,
};

export const stubMemories: MemoryPage = {
  items: [
    {
      id: "mem_01",
      content: "I prefer TypeScript with strict mode, and pnpm over npm.",
      kind: "preference",
      created_at: iso(2),
      provenance: {
        connection_id: "conn_cursor",
        client_name: "cursor",
        agent_id: null,
        initiated_by: "user",
      },
      superseded_count: 0,
    },
    {
      id: "mem_02",
      content:
        "Deploys go out Tuesdays and Thursdays only — never on a Friday.",
      kind: "decision",
      created_at: iso(5),
      provenance: {
        connection_id: "conn_cc",
        client_name: "claude-code",
        agent_id: "agent_cc_1",
        initiated_by: "agent",
      },
      superseded_count: 1,
    },
    {
      id: "mem_03",
      content:
        "Primary Postgres is on Neon, region us-east-2; pgvector enabled.",
      kind: "fact",
      created_at: iso(7),
      provenance: {
        connection_id: "conn_codex",
        client_name: "codex",
        agent_id: "agent_codex_1",
        initiated_by: "agent",
      },
      superseded_count: 0,
    },
  ],
  next_cursor: null,
};

export const stubHistory: MemoryHistory = {
  versions: [
    {
      id: "mem_02_v0",
      content: "Deploys go out on Tuesdays.",
      created_at: iso(20),
      provenance: {
        connection_id: "conn_cc",
        client_name: "claude-code",
        agent_id: "agent_cc_1",
        initiated_by: "agent",
      },
      tombstoned: false,
    },
    {
      id: "mem_02",
      content:
        "Deploys go out Tuesdays and Thursdays only — never on a Friday.",
      created_at: iso(5),
      provenance: {
        connection_id: "conn_cc",
        client_name: "claude-code",
        agent_id: "agent_cc_1",
        initiated_by: "agent",
      },
      tombstoned: false,
    },
  ],
};

export const stubSearch: SearchResponse = {
  results: stubMemories.items.map((m, i) => ({ ...m, score: 0.9 - i * 0.12 })),
};

export const stubSkills: SkillList = {
  skills: [
    {
      name: "release-checklist",
      description: "Steps to cut a release safely.",
      version: "1.4.0",
    },
    {
      name: "incident-response",
      description: "On-call runbook for production incidents.",
      version: "2.1.0",
    },
    {
      name: "code-review",
      description: "House style for reviewing pull requests.",
      version: "1.0.0",
    },
  ],
};

export function stubSkillDetail(name: string): SkillDetail {
  const match = stubSkills.skills.find((s) => s.name === name);
  return {
    name,
    description: match?.description ?? "",
    version: match?.version ?? "1.0.0",
    frontmatter: { name, description: match?.description ?? "" },
    body: `# ${name}\n\nEdit this skill in Markdown. Saving bumps the version per\nthe C5 rules.\n\n- step one\n- step two\n`,
    versions: ["1.0.0", match?.version ?? "1.0.0"].filter(
      (v, i, a) => a.indexOf(v) === i,
    ),
  };
}

export const stubConnections: ConnectionList = {
  connections: [
    {
      id: "conn_cc",
      client_name: "Claude Code",
      auth_mode: "oauth · cimd + pkce",
      scopes: ["memory:read", "skills:read"],
      writes_enabled: false,
      created_at: iso(30),
      revoked_at: null,
      last_seen_at: iso(0),
    },
    {
      id: "conn_cursor",
      client_name: "Cursor",
      auth_mode: "oauth · static client",
      scopes: ["memory:read", "memory:write"],
      writes_enabled: true,
      created_at: iso(21),
      revoked_at: null,
      last_seen_at: iso(1),
    },
    {
      id: "conn_codex",
      client_name: "Codex",
      auth_mode: "token · purse_pat_…KBXw",
      scopes: ["memory:read", "apis:use"],
      writes_enabled: false,
      created_at: iso(14),
      revoked_at: null,
      last_seen_at: iso(2),
    },
    {
      id: "conn_openclaw",
      client_name: "OpenClaw",
      auth_mode: "token · scoped read-only",
      scopes: ["memory:read"],
      writes_enabled: false,
      created_at: iso(40),
      revoked_at: null,
      last_seen_at: iso(30),
    },
  ],
};

export const stubAudit: AuditResponse = {
  entries: [
    {
      action: "memory.create",
      target_type: "memory",
      target_id: "mem_01",
      client_name: "cursor",
      agent_id: null,
      created_at: iso(2),
    },
    {
      action: "memory.supersede",
      target_type: "memory",
      target_id: "mem_02",
      client_name: "claude-code",
      agent_id: "agent_cc_1",
      created_at: iso(5),
    },
    {
      action: "connection.mint_pat",
      target_type: "connection",
      target_id: "conn_codex",
      client_name: null,
      agent_id: null,
      created_at: iso(14),
    },
    {
      action: "connection.revoke",
      target_type: "connection",
      target_id: "conn_old",
      client_name: null,
      agent_id: null,
      created_at: iso(18),
    },
  ],
};

export const stubExport = {
  purse_version: "0.3",
  exported_at: new Date(now).toISOString(),
  memories: stubMemories.items,
  skills: stubSkills.skills,
  apis: [],
};
