import type { NextRequest } from "next/server";
import { callBackend } from "@/lib/backend";

export async function GET(
  _req: NextRequest,
  ctx: { params: Promise<{ id: string }> },
) {
  const { id } = await ctx.params;
  return callBackend(`/web/memories/${encodeURIComponent(id)}/history`);
}
