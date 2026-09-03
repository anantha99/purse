import { callBackend } from "@/lib/backend";

export async function GET() {
  return callBackend("/web/workspace");
}
