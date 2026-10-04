export function sectionEnergyLabel(energy: number): string {
  if (energy < 0.35) return "차분하게";
  if (energy < 0.55) return "담백하게";
  if (energy < 0.72) return "조금씩 고조";
  return "힘 있게";
}

export function sectionGuidanceCopy(label: string, guidance: string, instrumental: boolean): string {
  const kind = label.toLowerCase().replace(/[\s-]+/g, "");
  if (guidance === "fuller groove, clear main hook" || /^(chorus|hook)/.test(kind) || kind === "themereprise") {
    return instrumental ? "주요 선율을 강조하고 편곡을 넓혀요." : "후렴을 강조하고 편곡을 넓혀요.";
  }
  if (guidance === "gradual lift, steady groove" || /^(prechorus|bridge|build)/.test(kind)) return "박자를 유지하며 조금씩 고조시켜요.";
  if (guidance === "light main instrument, brief entrance" || kind.startsWith("intro")) return "주요 악기로 가볍게 시작해요.";
  if (guidance === "gentle resolved ending" || kind.startsWith("outro")) return "자연스럽게 힘을 낮추며 마무리해요.";
  return instrumental ? "담백한 리듬으로 주요 선율이 또렷하게 들리도록 해요." : "담백한 리듬으로 목소리가 들어올 공간을 두어요.";
}
