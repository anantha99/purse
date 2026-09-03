import type { NextRequest } from "next/server";
import { callBackend, errorResponse } from "@/lib/backend";

export async function PATCH(
  req: NextRequest,
  ctx: { params: Promise<{ id: string }> },
) {
  const { id } = await ctx.params;
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return errorResponse(422, "VALIDATION", "Provide the updated content.");
  }
  return callBackend(`/web/memories/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body,
  });
}

export async function DELETE(
  _req: NextRequest,
  ctx: { params: Promise<{ id: string }> },
) {
  const { id } = await ctx.params;
  return callBackend(`/web/memories/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}
