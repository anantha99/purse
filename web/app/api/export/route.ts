import { callBackend } from "@/lib/backend";

// Raw passthrough so the browser receives the file with its Content-Disposition.
export async function GET() {
  return callBackend("/web/export", { raw: true });
}
