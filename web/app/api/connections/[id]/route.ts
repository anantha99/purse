import type { NextRequest } from "next/server";
import { callBackend } from "@/lib/backend";

export async function DELETE(
  _req: NextRequest,
  ctx: { params: Promise<{ id: string }> },
) {
  const { id } = await ctx.params;
  return callBackend(`/web/connections/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}
