import type { ProductionCatalog, ProductionRuleSelection } from "./shared.ts";

export function emptyProductionRules(): ProductionRuleSelection {
  return { version: 1, presetId: null, ruleIds: [] };
}

// IPC values are untrusted even when the renderer presents a fixed list of choices.
export function parseProductionRules(value: unknown, catalog: ProductionCatalog): ProductionRuleSelection {
  if (value === undefined) return emptyProductionRules();
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("제작 규칙 형식이 올바르지 않아요.");
  const input = value as Record<string, unknown>;
  if (Object.keys(input).some((key) => !["version", "presetId", "ruleIds"].includes(key)) || input.version !== 1
    || (input.presetId !== null && typeof input.presetId !== "string") || !Array.isArray(input.ruleIds)) {
    throw new Error("제작 규칙 형식이 올바르지 않아요.");
  }
  if (input.presetId !== null && !catalog.presets.some((preset) => preset.id === input.presetId)) throw new Error("알 수 없는 제작 프리셋이에요.");
  const known = new Set(catalog.rules.map((rule) => rule.id));
  if (input.ruleIds.length > known.size || input.ruleIds.some((id) => typeof id !== "string" || !known.has(id))
    || new Set(input.ruleIds).size !== input.ruleIds.length) throw new Error("알 수 없거나 중복된 제작 규칙이에요.");
  return { version: 1, presetId: input.presetId as string | null, ruleIds: [...input.ruleIds] as string[] };
}

// Presets select recommendations without changing a person's style, lyrics or metadata.
export function selectProductionPreset(presetId: string | null, catalog?: ProductionCatalog): ProductionRuleSelection {
  const preset = catalog?.presets.find((item) => item.id === presetId);
  return preset ? { version: 1, presetId: preset.id, ruleIds: [...preset.ruleIds] } : emptyProductionRules();
}

export function toggleProductionRule(selection: ProductionRuleSelection, id: string, checked: boolean): ProductionRuleSelection {
  return { ...selection, ruleIds: checked ? [...new Set([...selection.ruleIds, id])] : selection.ruleIds.filter((ruleId) => ruleId !== id) };
}

export function parseProductionBpm(value: string): number | null {
  const text = value.trim();
  if (!text) return null;
  const bpm = Number(text);
  if (!/^\d+$/.test(text) || !Number.isInteger(bpm) || bpm < 30 || bpm > 300) throw new Error("빠르기는 30~300 사이의 정수로 적어 주세요.");
  return bpm;
}

export type ProductionPreviewInput = {
  stylePrompt: string;
  instrumental: boolean;
  durationSeconds: number;
  bpm: string;
  keyScale: string;
  timeSignature: string;
  productionRules?: ProductionRuleSelection;
};

export function productionPreview(input: ProductionPreviewInput, catalog?: ProductionCatalog) {
  const selection = input.productionRules ?? emptyProductionRules();
  const preset = catalog?.presets.find((item) => item.id === selection.presetId);
  const plannedTiming = selection.ruleIds.includes("section-development") || (!input.instrumental && selection.ruleIds.includes("phrase-breathing"));
  const captions: Array<{ id: string; label: string; caption: string }> = [];
  for (const rule of catalog?.rules ?? []) {
    if (!selection.ruleIds.includes(rule.id)) continue;
    if (input.instrumental && ["clear-vocal", "phrase-breathing"].includes(rule.id)) continue;
    let caption = input.instrumental ? rule.instrumentalCaption ?? rule.caption : rule.caption;
    if (input.durationSeconds <= 45) caption = (input.instrumental ? rule.shortInstrumentalCaption : rule.shortCaption) ?? caption;
    if (caption) captions.push({ id: rule.id, label: rule.label, caption });
  }
  const baseStyle = input.stylePrompt.trim();
  const additions = [preset?.caption, ...captions.map((rule) => rule.caption)].filter((caption): caption is string => Boolean(caption) && !baseStyle.toLowerCase().includes(caption!.toLowerCase()));
  const stylePrompt = [baseStyle, ...additions].filter(Boolean).join(", ");
  const warnings: string[] = [];
  // Music3 supports detailed guidance. This preview contains style plus rules,
  // whereas the API limit includes the complete structured prompt. Character
  // counts do not estimate the model's token budget or imply omitted delivery.
  if (stylePrompt.length > (catalog?.captionBudgetCharacters ?? 16_000)) {
    warnings.push("제작 안내가 길어요. 중요한 분위기와 편곡 방향을 분명히 정리해 주세요.");
  }
  if (selection.ruleIds.includes("clean-production") && /\blo[ -]?fi\b|\bvinyl\b|\bdistort(?:ed|ion)?\b|\bcrackle\b|\btape hiss\b/i.test(input.stylePrompt)) {
    warnings.push("직접 적은 스타일의 거친 질감과 ‘깔끔한 음질’ 규칙이 함께 있어요. 원하는 질감에 맞게 한쪽을 조정하세요.");
  }
  if (preset && /\b(?:slow|fast|\d+\s*bpm)\b/i.test(input.stylePrompt)) {
    warnings.push("직접 적은 스타일에도 빠르기 표현이 있어요. 아래 보낼 BPM과 같은 방향인지 확인하세요.");
  }
  return {
    preset,
    captions,
    stylePrompt,
    bpm: input.bpm.trim() || (preset?.bpm ? String(preset.bpm) : plannedTiming ? "96" : ""),
    keyScale: input.keyScale.trim() || preset?.keyScale || "",
    timeSignature: input.timeSignature.trim() || (preset?.timeSignature === "4" ? "4/4" : preset?.timeSignature) || (plannedTiming ? "4/4" : ""),
    warnings,
  };
}
