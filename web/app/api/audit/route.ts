import type { NextRequest } from "next/server";
import { callBackend } from "@/lib/backend";

export async function GET(req: NextRequest) {
  return callBackend("/web/audit", { query: req.nextUrl.searchParams });
}
