import type { SongPlanResult } from "../shared.ts";
import { h, type Child } from "./dom.ts";
import { clock } from "./format.ts";
import { sectionEnergyLabel, sectionGuidanceCopy } from "./song-plan-copy.ts";

const labels: Record<string, string> = { intro: "전주", verse: "구절", chorus: "후렴", hook: "후렴", bridge: "연결 구간", outro: "마무리", theme: "주제", "theme reprise": "주제 반복", build: "고조", development: "전개", "pre-chorus": "후렴 앞" };

export function songPlanView(plan: SongPlanResult): Child {
  const breaks = plan.changes.filter((change) => change.kind === "line-break");
  return h("div", { class: "song-plan-view" },
    h("p", { class: "hint" }, `${plan.timing.bpm} BPM · ${plan.timing.timeSignature}박자 · 약 ${plan.timing.estimatedTotalBars.toFixed(1)}마디. 아래 전개와 호흡은 제작 안내예요. 실제 음원의 구간이나 발음과 다를 수 있어요.`),
    !plan.options.development && h("p", { class: "hint" }, "구간별 전개 규칙이 꺼져 있어 전개 표는 참고용으로 보여 드려요."),
    plan.options.development && h("p", { class: "hint" }, "제작할 때 구간 태그에 편곡과 강약 안내를 더해요. 직접 쓴 섹션 순서와 가사 원문은 남겨 둬요."),
    plan.arrangement.length > 0 && h("ol", { class: "song-plan-sections", "aria-label": "추천 곡 전개" }, plan.arrangement.map((section) => h("li", null,
      h("strong", null, labels[section.label.toLowerCase()] ?? section.label),
      h("span", { class: "hint" }, `${clock(section.startSeconds)}–${clock(section.endSeconds)} · ${section.bars}마디`),
      h("span", { class: "song-plan-energy" }, sectionEnergyLabel(section.energy)),
      h("p", { class: "hint" }, sectionGuidanceCopy(section.label, section.guidance, plan.options.instrumental)),
    ))),
    !plan.options.instrumental && h("details", { class: "disclosure" },
      h("summary", null, `가사 호흡 ${plan.phrases.length}구절${breaks.length ? ` · ${breaks.length}줄 줄바꿈 정리` : ""}`),
      h("p", { class: "hint" }, plan.options.breathing ? "제작할 때 긴 줄을 짧게 나누고 원문도 함께 보관해요. 단어와 가사 순서는 유지해요." : "호흡 규칙이 꺼져 있어 원문 줄바꿈을 유지해요."),
      h("pre", { class: "lyrics-view song-plan-lyrics" }, plan.lyricsPrepared),
      h("ul", { class: "production-selected" }, plan.phrases.slice(0, 24).map((phrase) => h("li", null, `${phrase.lineIndex}행 · ${phrase.syllables}음절 · 약 ${phrase.estimatedBars.toFixed(1)}마디 · 뒤 ${phrase.breathAfterBeats}박 쉼`))),
      plan.phrases.length > 24 && h("p", { class: "hint" }, "처음 24구절의 호흡을 표시했어요."),
      h("p", { class: "hint" }, "한글은 글자 기준, 영어는 발음 추정치예요."),
    ),
    plan.warnings.map((warning) => h("p", { class: "notice tone-warn" }, warning)),
    h("details", { class: "disclosure" }, h("summary", null, "제작 안내 원문 보기"),
      h("pre", { class: "lyrics-view" }, [...plan.arrangement.map((section) => `${section.label}: ${section.guidance}`), ...plan.guidance].join("\n"))),
  );
}
