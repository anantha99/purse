import type { NextRequest } from "next/server";
import { callBackend, errorResponse } from "@/lib/backend";

export async function POST(req: NextRequest) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return errorResponse(422, "VALIDATION", "Name the client for this token.");
  }
  return callBackend("/web/tokens", { method: "POST", body });
}
