import type { NextRequest } from "next/server";
import { callBackend, errorResponse } from "@/lib/backend";

export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ name: string }> },
) {
  const { name } = await ctx.params;
  return callBackend(`/web/skills/${encodeURIComponent(name)}`, {
    query: req.nextUrl.searchParams,
  });
}

export async function PUT(
  req: NextRequest,
  ctx: { params: Promise<{ name: string }> },
) {
  const { name } = await ctx.params;
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return errorResponse(422, "VALIDATION", "Provide the skill content.");
  }
  return callBackend(`/web/skills/${encodeURIComponent(name)}`, {
    method: "PUT",
    body,
  });
}
