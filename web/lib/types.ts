// Shapes mirror docs/web-api-contract.md. Kept deliberately narrow — only what
// the dashboard renders.

export type ApiError = { error: { code: string; message: string } };

export type InitiatedBy = "user" | "agent";

export type Provenance = {
  connection_id: string | null;
  client_name: string | null;
  agent_id: string | null;
  initiated_by: InitiatedBy;
};

export type MemoryKind = "preference" | "decision" | "fact" | "note" | string;

export type Memory = {
  id: string;
  content: string;
  kind: MemoryKind;
  created_at: string;
  provenance: Provenance;
  superseded_count: number;
};

export type MemoryPage = { items: Memory[]; next_cursor: string | null };

export type SearchResult = Memory & { score: number };
export type SearchResponse = { results: SearchResult[] };

export type MemoryVersion = {
  id: string;
  content: string;
  created_at: string;
  provenance: Provenance;
  tombstoned: boolean;
};
export type MemoryHistory = { versions: MemoryVersion[] };

export type SkillSummary = { name: string; description: string; version: string };
export type SkillList = { skills: SkillSummary[] };
export type SkillDetail = {
  name: string;
  description: string;
  version: string;
  frontmatter: Record<string, unknown>;
  body: string;
  versions: string[];
};

export type AuthMode = string; // e.g. "oauth · cimd + pkce", "token · purse_pat_…KBXw"

export type Connection = {
  id: string;
  client_name: string;
  auth_mode: AuthMode;
  scopes: string[];
  writes_enabled: boolean;
  created_at: string;
  revoked_at: string | null;
  last_seen_at?: string | null;
};
export type ConnectionList = { connections: Connection[] };

export type MintTokenRequest = {
  client_name: string;
  scopes: string[];
  writes_enabled: boolean;
};
export type MintTokenResponse = {
  connection: {
    id: string;
    client_name: string;
    scopes: string[];
    writes_enabled: boolean;
  };
  token: string;
};

export type AuditEntry = {
  action: string;
  target_type: string;
  target_id: string;
  client_name: string | null;
  agent_id: string | null;
  created_at: string;
};
export type AuditResponse = { entries: AuditEntry[] };

export type WorkspaceCounts = {
  memories: number;
  skills: number;
  apis: number;
  connections: number;
};

export type SessionInfo = {
  user: { email: string };
  workspace: { id: string; name: string };
  writes_enabled_default: boolean;
};

export type LoginResponse = {
  user: { email: string };
  workspace: { id: string; name: string };
};
