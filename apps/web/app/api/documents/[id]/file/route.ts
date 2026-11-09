import { proxyToApi } from "@/lib/proxy";

interface Ctx {
  params: { id: string };
}

// Streams the stored evidence file for the checklist preview drawer
// (inline for PDF / images / text, attachment otherwise or with ?download=1).
export async function GET(req: Request, { params }: Ctx) {
  return proxyToApi(req, `/v1/documents/${params.id}/file`);
}
