import type { NextRequest } from "next/server";
import { callBackend, errorResponse } from "@/lib/backend";

export async function GET(req: NextRequest) {
  return callBackend("/web/memories", {
    query: req.nextUrl.searchParams,
  });
}

export async function POST(req: NextRequest) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return errorResponse(422, "VALIDATION", "Add some content for this memory.");
  }
  return callBackend("/web/memories", { method: "POST", body });
}
