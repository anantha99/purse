// Server-only BFF proxy. Route handlers under app/api/* call these helpers.
// The browser never reaches the Python backend directly: it calls /api/*, and
// these helpers attach the session token (from the httpOnly cookie) as
// `Authorization: Bearer <token>` when talking to `${PURSE_BACKEND_URL}/web/*`.
import "server-only";
import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { SESSION_COOKIE } from "./session";
import {
  stubAudit,
  stubConnections,
  stubExport,
  stubHistory,
  stubMemories,
  stubSearch,
  stubSession,
  stubSkillDetail,
  stubSkills,
  stubWorkspace,
} from "./stubs";

export function backendConfigured(): boolean {
  return Boolean(process.env.PURSE_BACKEND_URL);
}

function stubFallbackAllowed(): boolean {
  return process.env.PURSE_ALLOW_STUB_FALLBACK === "1";
}

export async function getSessionToken(): Promise<string | null> {
  const store = await cookies();
  return store.get(SESSION_COOKIE)?.value ?? null;
}

export type BackendResult = {
  status: number;
  data: unknown;
  headers?: Record<string, string>;
};

function errorBody(code: string, message: string) {
  return { error: { code, message } };
}

/** JSON error -> NextResponse, preserving the { error: { code, message } } shape. */
export function errorResponse(status: number, code: string, message: string) {
  return NextResponse.json(errorBody(code, message), { status });
}

type CallOpts = {
  method?: string;
  body?: unknown;
  query?: URLSearchParams | null;
  auth?: boolean;
  raw?: boolean;
};

/**
 * Call the backend `/web` API, or serve a stub. Returns a NextResponse ready to
 * return from a route handler.
 */
export async function callBackend(
  webPath: string,
  opts: CallOpts = {},
): Promise<NextResponse> {
  const { method = "GET", body, query = null, auth = true, raw = false } = opts;

  let token: string | null = null;
  if (auth) {
    token = await getSessionToken();
    if (!token) {
      return errorResponse(401, "UNAUTHENTICATED", "Sign in to continue.");
    }
  }

  const qs = query && [...query.keys()].length ? `?${query.toString()}` : "";

  if (!backendConfigured()) {
    return stubResponse(method, webPath, query, body, raw);
  }

  const base = process.env.PURSE_BACKEND_URL!.replace(/\/$/, "");
  const url = `${base}${webPath}${qs}`;

  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const init: RequestInit = { method, headers, cache: "no-store" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    if (stubFallbackAllowed()) {
      return stubResponse(method, webPath, query, body, raw);
    }
    return errorResponse(
      502,
      "BACKEND_UNREACHABLE",
      "Can't reach the Purse backend. Check PURSE_BACKEND_URL and that the server is running.",
    );
  }

  if (raw) {
    const buf = await res.arrayBuffer();
    const out = new NextResponse(buf, { status: res.status });
    const ct = res.headers.get("content-type");
    const cd = res.headers.get("content-disposition");
    if (ct) out.headers.set("content-type", ct);
    if (cd) out.headers.set("content-disposition", cd);
    return out;
  }

  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: { code: "BAD_GATEWAY", message: "Malformed backend response." } };
    }
  }
  if (res.status === 204 || data === null) {
    return new NextResponse(null, { status: res.status });
  }
  return NextResponse.json(data, { status: res.status });
}

// --------------------------------------------------------------------------
// Stub router — mirrors the contract when no backend is configured/reachable.
// --------------------------------------------------------------------------
function stubResponse(
  method: string,
  webPath: string,
  query: URLSearchParams | null,
  body: unknown,
  raw: boolean,
): NextResponse {
  const m = method.toUpperCase();
  const path = webPath;

  // Session
  if (m === "POST" && path === "/web/login") {
    const b = (body ?? {}) as { password?: string };
    if (!b.password) {
      return errorResponse(401, "INVALID_CREDENTIALS", "Password is required.");
    }
    return NextResponse.json({
      user: stubSession.user,
      workspace: stubSession.workspace,
      session_token: "stub-session-token",
    });
  }
  if (m === "POST" && path === "/web/logout") {
    return new NextResponse(null, { status: 204 });
  }
  if (m === "GET" && path === "/web/session") {
    return NextResponse.json(stubSession);
  }
  if (m === "GET" && path === "/web/workspace") {
    return NextResponse.json(stubWorkspace);
  }

  // Memories
  if (m === "GET" && path === "/web/memories") {
    return NextResponse.json(stubMemories);
  }
  if (m === "GET" && path === "/web/memories/search") {
    const q = query?.get("q")?.toLowerCase() ?? "";
    const results = q
      ? stubSearch.results.filter((r) => r.content.toLowerCase().includes(q))
      : stubSearch.results;
    return NextResponse.json({ results });
  }
  const histMatch = path.match(/^\/web\/memories\/([^/]+)\/history$/);
  if (m === "GET" && histMatch) {
    return NextResponse.json(stubHistory);
  }
  if (m === "POST" && path === "/web/memories") {
    const b = (body ?? {}) as {
      content?: string;
      kind?: string;
      initiated_by?: string;
    };
    return NextResponse.json(
      {
        id: `mem_${Math.random().toString(36).slice(2, 8)}`,
        content: b.content ?? "",
        kind: b.kind ?? "note",
        created_at: new Date().toISOString(),
        provenance: {
          connection_id: null,
          client_name: "web",
          agent_id: null,
          initiated_by: b.initiated_by ?? "user",
        },
        superseded_count: 0,
      },
      { status: 201 },
    );
  }
  const memIdMatch = path.match(/^\/web\/memories\/([^/]+)$/);
  if (memIdMatch) {
    const id = memIdMatch[1];
    if (m === "PATCH") {
      const b = (body ?? {}) as { content?: string };
      return NextResponse.json({
        id: `mem_${Math.random().toString(36).slice(2, 8)}`,
        content: b.content ?? "",
        kind: "note",
        created_at: new Date().toISOString(),
        provenance: {
          connection_id: null,
          client_name: "web",
          agent_id: null,
          initiated_by: "user",
        },
        superseded_count: 1,
      });
    }
    if (m === "DELETE") {
      return NextResponse.json({ id, deleted: true });
    }
  }

  // Skills
  if (m === "GET" && path === "/web/skills") {
    return NextResponse.json(stubSkills);
  }
  const skillMatch = path.match(/^\/web\/skills\/([^/]+)$/);
  if (skillMatch) {
    const name = decodeURIComponent(skillMatch[1]);
    if (m === "GET") return NextResponse.json(stubSkillDetail(name));
    if (m === "PUT") {
      const cur = stubSkillDetail(name).version;
      const [maj, min] = cur.split(".");
      const bumped = `${maj}.${Number(min ?? 0) + 1}.0`;
      return NextResponse.json({ name, version: bumped });
    }
  }

  // Connections
  if (m === "GET" && path === "/web/connections") {
    return NextResponse.json(stubConnections);
  }
  const connMatch = path.match(/^\/web\/connections\/([^/]+)$/);
  if (m === "DELETE" && connMatch) {
    return NextResponse.json({ id: connMatch[1], revoked: true });
  }

  // Tokens
  if (m === "POST" && path === "/web/tokens") {
    const b = (body ?? {}) as {
      client_name?: string;
      scopes?: string[];
      writes_enabled?: boolean;
    };
    return NextResponse.json(
      {
        connection: {
          id: `conn_${Math.random().toString(36).slice(2, 8)}`,
          client_name: b.client_name ?? "new client",
          scopes: b.scopes ?? ["memory:read"],
          writes_enabled: Boolean(b.writes_enabled),
        },
        token: `purse_pat_${Math.random().toString(36).slice(2, 10)}${Math.random()
          .toString(36)
          .slice(2, 10)}`,
      },
      { status: 201 },
    );
  }

  // Audit
  if (m === "GET" && path === "/web/audit") {
    return NextResponse.json(stubAudit);
  }

  // Export
  if (m === "GET" && path === "/web/export") {
    const json = JSON.stringify(stubExport, null, 2);
    if (raw) {
      const out = new NextResponse(json, { status: 200 });
      out.headers.set("content-type", "application/json");
      out.headers.set(
        "content-disposition",
        'attachment; filename="purse-export.json"',
      );
      return out;
    }
    return NextResponse.json(stubExport);
  }

  return errorResponse(404, "NOT_FOUND", "No stub for this route.");
}
