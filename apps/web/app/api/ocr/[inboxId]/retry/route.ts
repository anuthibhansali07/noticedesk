import { proxyToApi } from "@/lib/proxy";

// Re-runs OCR (then parse + route) on an inbox document whose OCR failed.
export async function POST(
  req: Request,
  { params }: { params: { inboxId: string } },
) {
  return proxyToApi(req, `/v1/documents/inbox/${params.inboxId}/retry-ocr`);
}
