import { NextResponse } from "next/server";
import { callBackend } from "@/lib/backend";
import { SESSION_COOKIE, sessionCookieOptions } from "@/lib/session";

export async function POST() {
  // Best-effort backend notify; the token is stateless so the real logout is
  // clearing the cookie here.
  try {
    await callBackend("/web/logout", { method: "POST", auth: false });
  } catch {
    // ignore
  }
  const out = new NextResponse(null, { status: 204 });
  out.cookies.set(SESSION_COOKIE, "", sessionCookieOptions(0));
  return out;
}
