import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { callBackend, errorResponse } from "@/lib/backend";
import {
  SESSION_COOKIE,
  SESSION_MAX_AGE,
  sessionCookieOptions,
} from "@/lib/session";

export async function POST(req: NextRequest) {
  let body: { password?: string };
  try {
    body = await req.json();
  } catch {
    return errorResponse(422, "VALIDATION", "Enter your password to sign in.");
  }
  if (!body?.password) {
    return errorResponse(422, "VALIDATION", "Enter your password to sign in.");
  }

  const res = await callBackend("/web/login", {
    method: "POST",
    body: { password: body.password },
    auth: false,
  });

  if (res.status !== 200) {
    return res; // pass the backend error shape straight through
  }

  const data = (await res.json()) as {
    user: { email: string };
    workspace: { id: string; name: string };
    session_token: string;
  };

  const out = NextResponse.json({ user: data.user, workspace: data.workspace });
  // The token lives ONLY in this httpOnly cookie on the frontend origin.
  out.cookies.set(
    SESSION_COOKIE,
    data.session_token,
    sessionCookieOptions(SESSION_MAX_AGE),
  );
  return out;
}
