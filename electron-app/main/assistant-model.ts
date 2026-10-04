import type { AssistantKind } from "../shared.ts";
import { requireLoopbackUrl } from "./files.ts";

type Assistant = { kind: AssistantKind; baseUrl: string; model: string };

// Ollama's keep_alive=0 response must confirm completion before ACE can claim memory.
export async function unloadAssistant(assistant: Assistant, request: typeof fetch = fetch): Promise<void> {
  if (assistant.kind !== "ollama" || !assistant.model) return;
  const base = requireLoopbackUrl(assistant.baseUrl, "AI 도우미").replace(/\/$/, "");
  const canonical = (name: string) => name.split("/").at(-1)?.includes(":") ? name : `${name}:latest`;
  const resident = async () => {
    const response = await request(`${base}/api/ps`, { signal: AbortSignal.timeout(3000), redirect: "error" });
    if (!response.ok) throw new Error(`AI 도우미의 메모리 상태를 확인하지 못했어요 (HTTP ${response.status}).`);
    const body = await response.json() as { models?: { name?: string; model?: string }[] };
    if (!Array.isArray(body.models)) throw new Error("AI 도우미의 메모리 상태 응답을 읽지 못했어요.");
    return body.models.some((item) => [item.name, item.model].some((name) => name && canonical(name) === canonical(assistant.model)));
  };
  if (!await resident()) return;
  const response = await request(`${base}/api/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: assistant.model, keep_alive: 0 }),
    signal: AbortSignal.timeout(15000),
    redirect: "error",
  });
  if (!response.ok) throw new Error(`AI 도우미를 메모리에서 내리지 못했어요 (HTTP ${response.status}).`);
  const body = await response.json() as { done?: boolean };
  if (body.done !== true || await resident()) throw new Error("AI 도우미가 아직 메모리에 있어요. 도우미를 내린 뒤 음악 엔진을 다시 켜세요.");
}
