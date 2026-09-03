// Browser-side typed client. Talks ONLY to the Next.js BFF (/api/*), never to
// the Python backend. Normalizes the { error: { code, message } } shape into a
// thrown ApiClientError the UI can render with friendly copy.

import type {
  AuditResponse,
  Connection,
  ConnectionList,
  LoginResponse,
  MemoryHistory,
  MemoryPage,
  MintTokenRequest,
  MintTokenResponse,
  SearchResponse,
  SessionInfo,
  SkillDetail,
  SkillList,
  WorkspaceCounts,
  Memory,
} from "./types";

export class ApiClientError extends Error {
  code: string;
  status: number;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      ...init,
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    throw new ApiClientError(
      0,
      "NETWORK",
      "Network error — the dashboard couldn't be reached. Check your connection and retry.",
    );
  }

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }

  if (!res.ok) {
    const err = (data as { error?: { code?: string; message?: string } })?.error;
    throw new ApiClientError(
      res.status,
      err?.code ?? "ERROR",
      err?.message ?? friendlyStatus(res.status),
    );
  }
  return data as T;
}

function friendlyStatus(status: number): string {
  switch (status) {
    case 401:
      return "Your session expired. Sign in again to continue.";
    case 404:
      return "That item no longer exists.";
    case 413:
      return "That's too large to store. Try trimming the content.";
    case 422:
      return "Some fields need fixing before this can be saved.";
    case 429:
      return "Too many requests — wait a moment and try again.";
    default:
      return "Something went wrong. Try again in a moment.";
  }
}

export const api = {
  // Session
  login: (password: string) =>
    request<LoginResponse>("/login", {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
  logout: () => request<void>("/logout", { method: "POST" }),
  session: () => request<SessionInfo>("/session"),

  // Workspace
  workspace: () => request<WorkspaceCounts>("/workspace"),

  // Memories
  memories: (params?: {
    cursor?: string;
    limit?: number;
    kind?: string;
    initiated_by?: string;
  }) => request<MemoryPage>(`/memories${qs(params)}`),
  searchMemories: (q: string, limit?: number) =>
    request<SearchResponse>(`/memories/search${qs({ q, limit })}`),
  memoryHistory: (id: string) =>
    request<MemoryHistory>(`/memories/${encodeURIComponent(id)}/history`),
  addMemory: (body: { content: string; kind: string; initiated_by?: string }) =>
    request<Memory>("/memories", { method: "POST", body: JSON.stringify(body) }),
  editMemory: (id: string, content: string) =>
    request<Memory>(`/memories/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ content }),
    }),
  deleteMemory: (id: string) =>
    request<{ id: string; deleted: boolean }>(
      `/memories/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),

  // Skills
  skills: () => request<SkillList>("/skills"),
  skill: (name: string, version?: string) =>
    request<SkillDetail>(`/skills/${encodeURIComponent(name)}${qs({ version })}`),
  saveSkill: (name: string, content: string) =>
    request<{ name: string; version: string }>(
      `/skills/${encodeURIComponent(name)}`,
      { method: "PUT", body: JSON.stringify({ content }) },
    ),

  // Connections
  connections: () => request<ConnectionList>("/connections"),
  revokeConnection: (id: string) =>
    request<{ id: string; revoked: boolean }>(
      `/connections/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),

  // Tokens
  mintToken: (body: MintTokenRequest) =>
    request<MintTokenResponse>("/tokens", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Audit
  audit: (limit = 100) => request<AuditResponse>(`/audit${qs({ limit })}`),
};

export type { Connection };

function qs(params?: Record<string, string | number | undefined>): string {
  if (!params) return "";
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}
