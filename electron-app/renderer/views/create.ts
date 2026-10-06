import { api, applySong, engineReady, go, startEngine } from "../actions.ts";
import { byId, h, icon, mount } from "../dom.ts";
import { clock, hasTag, lengthLabel, splitTags, toggleTag } from "../format.ts";
import { beginSongOpen, finishSongOpen, get, set, type CreateDraft } from "../store.ts";
import { DraftEdits } from "../draft-edits.ts";
import type { DraftResult, SongPlanInput, SongPlanResult } from "../../shared.ts";
import { toast, withBusy } from "../ui.ts";
import { emptyProductionRules, parseProductionBpm, productionPreview, selectProductionPreset, toggleProductionRule } from "../../production-rules.ts";
import { SongPlanPreview } from "../song-plan-preview.ts";
import { songPlanView } from "../song-plan-view.ts";
import { INSTRUMENTS, addInstrument, hasInstrument, removeInstrument } from "../instruments.ts";

// The create screen reads top to bottom as three steps: say what you want, let the
// assistant draft it, adjust the draft. Everything a musician would tune lives under
// one closed "세부 조정" so the first screen is a single question.

// Plain-Korean labels for the English tags the music model understands. The chip shows the
// word a non-musician would use; the tooltip shows the exact tag that is sent.
const STYLE_GROUPS: Array<{ name: string; tags: Array<[string, string]> }> = [
  {
    name: "장르",
    tags: [
      ["감성 힙합", "melodic hip hop"], ["시티팝", "city pop"], ["K-팝 댄스", "K-pop dance"], ["R&B", "R&B"], ["인디 팝", "indie pop"],
      ["로파이", "lo-fi hip hop"], ["밴드 록", "band rock"], ["일렉트로닉", "electronic"], ["재즈", "jazz"], ["어쿠스틱 포크", "acoustic folk"],
      ["발라드", "Korean ballad"], ["영화 음악", "cinematic orchestral"],
    ],
  },
  {
    name: "분위기",
    tags: [
      ["신나는", "energetic"], ["그루비한", "groovy"], ["희망찬", "uplifting"], ["몽환적인", "dreamy"], ["그리운", "nostalgic"],
      ["애절한", "melancholic"], ["설레는", "romantic"], ["웅장한", "epic"], ["따뜻한", "warm"], ["잔잔한", "calm"],
    ],
  },
  {
    name: "목소리",
    tags: [
      ["남성 보컬", "male vocal"], ["여성 보컬", "female vocal"], ["랩", "rap verses"], ["또렷한 발음", "clear diction"],
      ["힘 있게", "powerful vocal"], ["속삭이듯", "breathy vocal"], ["화음 코러스", "vocal harmonies"],
    ],
  },
  {
    name: "소리와 편곡",
    tags: [
      ["끊기지 않는 비트", "continuous drum groove"], ["드럼 강하게", "punchy drums"], ["드럼 약하게", "light drums"],
      ["풍성하게", "lush arrangement"], ["깔끔한 믹스", "clean mix"], ["빈티지", "vintage warmth"],
    ],
  },
  {
    name: "빠르기",
    tags: [["빠르게", "upbeat"], ["중간 빠르기", "mid-tempo"], ["느리게", "slow tempo"]],
  },
];

const EXAMPLES = [
  "늦은 밤 도시를 달리는 감성 힙합. 남자 목소리, 비트와 피아노가 끝까지 이어지게",
  "여름 밤 드라이브에 어울리는 신나는 시티팝, 여자 목소리, 그루비한 베이스",
  "카페에서 흘러나오는 따뜻한 어쿠스틱 연주곡",
  "졸업식에서 부르는 희망찬 밴드 록, 떼창하기 좋은 후렴",
];

const SECTIONS: Array<[string, string]> = [
  ["[Intro]", "전주 — 노래가 시작되기 전 부분"],
  ["[Verse]", "벌스 — 이야기를 풀어 가는 부분"],
  ["[Pre-Chorus]", "프리코러스 — 후렴 직전에 분위기를 끌어올리는 부분"],
  ["[Chorus]", "후렴 — 가장 기억에 남는, 반복되는 부분"],
  ["[Bridge]", "브리지 — 후반에 한 번 분위기를 바꾸는 부분"],
  ["[Outro]", "아웃트로 — 곡을 마무리하는 부분"],
];

const DURATIONS = [30, 60, 120, 180, 240, 300];
const INSTRUMENTAL = "[Instrumental]";
type Voice = "ko" | "en" | "inst";

type Refs = {
  description: HTMLTextAreaElement;
  title: HTMLInputElement;
  style: HTMLTextAreaElement;
  lyrics: HTMLTextAreaElement;
  startLyrics: HTMLTextAreaElement;
  startLyricsBox: HTMLDetailsElement;
  lyricsField: HTMLElement;
  lyricsInstrumental: HTMLElement;
  chips: HTMLButtonElement[];
  voice: HTMLElement;
  instruments: HTMLElement;
  styleHint: HTMLElement;
  durations: HTMLElement;
  versions: HTMLElement;
  draftButton: HTMLButtonElement;
  draftStatus: HTMLElement;
  draftSection: HTMLElement;
  startManual: HTMLElement;
  bpm: HTMLInputElement;
  key: HTMLInputElement;
  meter: HTMLSelectElement;
  lyricsHint: HTMLElement;
  presets: HTMLElement;
  presetHint: HTMLElement;
  productionCheckboxes: Map<string, HTMLInputElement>;
  tuneSummary: HTMLElement;
  songPlanButton: HTMLButtonElement;
  songPlanStatus: HTMLElement;
  sendPreview: HTMLElement;
  bar: HTMLElement;
  createButton: HTMLButtonElement;
};

let refs: Refs | null = null;
const draftEdits = new DraftEdits();
let drafting = false;
let planningSong = false;
let creating = false;
// Step 2 follows the draft unless the person opened it ("직접 쓸게요") or closed it (×).
let draftOpen: boolean | null = null;
const songPlanPreview = new SongPlanPreview();

export function blankDraft(): CreateDraft {
  const settings = get()?.settings;
  return {
    description: "",
    instrumental: false,
    title: "",
    stylePrompt: "",
    lyrics: "",
    durationSeconds: settings?.defaultDurationSeconds ?? 60,
    versions: settings?.defaultVersions ?? 1,
    bpm: "",
    keyScale: "",
    timeSignature: "",
    drafted: false,
    productionRules: emptyProductionRules(),
    vocalLanguage: "ko",
  };
}

function patch(change: Partial<CreateDraft>): void {
  draftEdits.edited(change);
  songPlanPreview.edited();
  set({ create: { ...get().create, ...change } });
}

function flash(element: HTMLElement): void {
  element.classList.remove("is-filled");
  void element.offsetWidth;
  element.classList.add("is-filled");
}

function fallbackTitle(description: string): string {
  const head = description.split(/[,.\n·]/)[0]?.trim() ?? "";
  return head.slice(0, 24) || "새 곡";
}

function voiceOf(draft: CreateDraft): Voice {
  return draft.instrumental ? "inst" : draft.vocalLanguage ?? "ko";
}

// Lyrics alone do not open step 2: they can be pasted in step 1 before asking for a draft.
function draftVisible(draft: CreateDraft): boolean {
  return draftOpen ?? (draft.drafted || Boolean(draft.title.trim() || draft.stylePrompt.trim()));
}

function hasDraftContent(draft: CreateDraft): boolean {
  return Boolean(draft.title.trim() || draft.stylePrompt.trim() || draft.drafted);
}

function pickedInstruments(draft: CreateDraft) {
  return INSTRUMENTS.filter((item) => (draft.instruments ?? []).includes(item.tag));
}

// What is sent: the written style plus any picked instrument it does not mention yet.
function sentDraft(draft: CreateDraft): CreateDraft {
  return { ...draft, stylePrompt: pickedInstruments(draft).reduce((caption, instrument) => addInstrument(caption, instrument), draft.stylePrompt.trim()) };
}

function insertSection(tag: string): void {
  if (!refs) return;
  const area = refs.lyrics;
  const { selectionStart, value } = area;
  const before = value.slice(0, selectionStart);
  const after = value.slice(selectionStart);
  const prefix = before && !before.endsWith("\n\n") ? (before.endsWith("\n") ? "\n" : "\n\n") : "";
  const insertion = `${prefix}${tag}\n`;
  area.value = before + insertion + after;
  const caret = before.length + insertion.length;
  area.setSelectionRange(caret, caret);
  area.focus();
  refs.startLyrics.value = area.value;
  patch({ lyrics: area.value });
  syncCreate();
}

async function requestDraft(): Promise<void> {
  if (!refs || drafting || refs.draftButton.disabled) return;
  const draft = get().create;
  if (!draft.description.trim()) {
    toast("어떤 곡인지 한 줄이라도 적어 주세요.", { tone: "error" });
    refs.description.focus();
    return;
  }
  const llm = get().settings.assistant.kind !== "rules";
  drafting = true;
  const ticket = draftEdits.begin(draft);
  mount(refs.draftStatus, h("span", { class: "draft-note" }, h("span", { class: "spinner", "aria-hidden": "true" }),
    llm ? "로컬 LLM이 제목·스타일·가사를 쓰고 있어요. 처음에는 30초쯤 걸릴 수 있어요." : "제목·스타일·가사 초안을 채우고 있어요."));
  let result: DraftResult | undefined;
  try {
    // Picked instruments travel with the description so the assistant writes around them.
    const instruments = pickedInstruments(draft).map((item) => item.label);
    const query = instruments.length ? `${draft.description.trim()} · 악기: ${instruments.join(", ")}`.slice(0, 1000) : draft.description;
    result = await withBusy(refs.draftButton, "초안을 쓰는 중", () => api.draft(query, draft.instrumental, draft.durationSeconds, draft.productionRules, draft.vocalLanguage ?? "ko"));
  } finally {
    drafting = false;
    syncCreate();
  }
  if (!refs || !draftEdits.isLatest(ticket)) return;
  refs.draftStatus.replaceChildren();
  if (!result) {
    syncCreate();
    return;
  }
  const change = draftEdits.merge(ticket, get().create, result, fallbackTitle(draft.description));
  if (!change) return;
  // The drafted style always carries the instruments picked in step 1.
  if (change.stylePrompt !== undefined) {
    for (const instrument of pickedInstruments(get().create)) change.stylePrompt = addInstrument(change.stylePrompt, instrument);
  }
  songPlanPreview.edited();
  draftOpen = null;
  set({ create: { ...get().create, ...change } });
  const latest = get().create;
  refs.style.value = latest.stylePrompt;
  if (!latest.instrumental) refs.lyrics.value = refs.startLyrics.value = latest.lyrics;
  refs.title.value = latest.title;
  syncCreate();
  if (change.title !== undefined) flash(refs.title);
  if (change.stylePrompt !== undefined) flash(refs.style);
  if (change.lyrics !== undefined) flash(refs.lyrics);
  const source = result.source === "llm" ? `로컬 LLM(${result.sourceModel})이 쓴 초안이에요.` : "기본 규칙으로 채운 초안이에요.";
  mount(
    refs.draftStatus,
    h("span", { class: "draft-note is-done" }, icon("check", 14), source, " 아래에서 바로 고칠 수 있어요."),
    change.lyrics === undefined && !latest.instrumental && latest.lyrics.trim() && h("span", { class: "draft-note" }, icon("info", 14), "직접 넣은 가사는 그대로 두고 제목과 스타일만 채웠어요."),
    result.notes.map((note) => h("span", { class: "draft-note" }, icon("info", 14), note)),
  );
  refs.draftSection.scrollIntoView({ block: "start", behavior: "smooth" });
}

function songPlanInput(draft: CreateDraft): SongPlanInput {
  const preview = productionPreview(draft, get().productionCatalog);
  const rules = draft.productionRules ?? emptyProductionRules();
  return { lyrics: draft.instrumental ? INSTRUMENTAL : draft.lyrics, durationSeconds: draft.durationSeconds,
    bpm: parseProductionBpm(preview.bpm), timeSignature: preview.timeSignature || null, presetId: rules.presetId,
    development: rules.ruleIds.includes("section-development"), breathing: !draft.instrumental && rules.ruleIds.includes("phrase-breathing"), instrumental: draft.instrumental };
}

async function requestSongPlan(): Promise<SongPlanResult | null> {
  if (planningSong) return null;
  let input: SongPlanInput;
  try { input = songPlanInput(get().create); } catch (error) {
    toast(error instanceof Error ? error.message : String(error), { tone: "error" });
    return null;
  }
  const cached = songPlanPreview.current(input);
  if (cached) return cached;
  const ticket = songPlanPreview.begin(input);
  planningSong = true;
  syncCreate();
  try {
    const plan = await api.songPlan(input);
    if (!songPlanPreview.complete(ticket, plan)) return null;
    return plan;
  } catch (error) {
    toast(error instanceof Error ? error.message : String(error), { tone: "error" });
    return null;
  } finally {
    planningSong = false;
    syncCreate();
  }
}

// Why the create button cannot run yet, in the order a person would fix it.
function blocker(draft: CreateDraft): string | null {
  if (!productionPreview(sentDraft(draft), get().productionCatalog).stylePrompt) {
    return draftVisible(draft) ? "스타일을 적거나 장르 추천을 골라 주세요" : "먼저 AI 초안을 받거나 직접 써 주세요";
  }
  if (!draft.instrumental && !draft.lyrics.trim()) return "가사를 적거나 ‘연주곡’을 골라 주세요";
  if (!engineReady()) return get().engine.state === "starting" ? "음악 엔진을 켜는 중이에요" : "음악 엔진이 꺼져 있어요";
  return null;
}

async function submit(button: HTMLButtonElement): Promise<void> {
  if (button.disabled || creating || planningSong) return;
  const draft = sentDraft(get().create);
  const lyrics = draft.instrumental ? INSTRUMENTAL : draft.lyrics.trim();
  if (!productionPreview(draft, get().productionCatalog).stylePrompt) {
    toast("스타일을 한 줄 적거나 장르 추천을 골라 주세요.", { tone: "error" });
    refs?.style.focus();
    return;
  }
  if (!lyrics) {
    toast("가사를 적거나 ‘연주곡’을 골라 주세요.", { tone: "error" });
    refs?.lyrics.focus();
    return;
  }
  let bpm: number | null;
  try {
    bpm = parseProductionBpm(draft.bpm);
  } catch (error) {
    toast(error instanceof Error ? error.message : String(error), { tone: "error" });
    refs?.bpm.focus();
    return;
  }
  creating = true;
  syncCreate();
  const plan = await requestSongPlan();
  if (!plan) { creating = false; syncCreate(); return; }
  let currentPlan = false;
  try {
    currentPlan = get().view === "create" && songPlanPreview.current(songPlanInput(get().create)) === plan;
  } catch {
    // An input can become invalid while the asynchronous preview is finishing.
    // Treat that response as stale and always release the create button below.
  }
  if (!currentPlan) {
    creating = false; syncCreate();
    toast("가사나 제작 규칙이 바뀌었어요. 다시 확인한 뒤 만들어 주세요.", { tone: "error" });
    return;
  }
  const request = beginSongOpen();
  const state = await withBusy(button, "만드는 중", () =>
    api.createSong({
      title: draft.title.trim() || fallbackTitle(draft.description),
      stylePrompt: draft.stylePrompt.trim(),
      lyrics,
      durationSeconds: draft.durationSeconds,
      versions: draft.versions,
      bpm,
      keyScale: draft.keyScale.trim() || null,
      timeSignature: draft.timeSignature.trim() || null,
      productionRules: draft.productionRules ?? emptyProductionRules(),
    }),
  );
  creating = false;
  syncCreate();
  if (!finishSongOpen(request) || !state) return;
  applySong(state);
  set({ create: blankDraft() });
  draftEdits.reset();
  songPlanPreview.reset();
  draftOpen = null;
  refs = null;
  go("studio");
  toast(`버전 ${draft.versions}개를 만들기 시작했어요. 끝나면 자동으로 검사하고 추천본을 골라 드려요.`, { tone: "ok" });
}

function segmented<T extends string | number>(values: T[], current: T, label: (value: T) => string, onPick: (value: T) => void, tip?: (value: T) => string | undefined) {
  const all = values.includes(current) ? values : [...values, current];
  return all.map((value) =>
    h(
      "button",
      {
        type: "button",
        class: value === current ? "is-active" : "",
        "aria-pressed": value === current ? "true" : "false",
        "data-tip": tip?.(value),
        onClick: () => onPick(value),
      },
      label(value),
    ),
  );
}

function build(): void {
  const draft = get().create;
  const root = byId("view-create");
  const description = h("textarea", {
    class: "prompt-input",
    rows: 3,
    maxlength: 1000,
    placeholder: "예: 늦은 밤 도시를 달리는 감성 힙합. 남자 목소리, 비트와 피아노가 끝까지 이어지게",
    value: draft.description,
    onInput: (event: Event) => patch({ description: (event.target as HTMLTextAreaElement).value }),
    onKeydown: (event: KeyboardEvent) => {
      if (event.key === "Enter" && !event.shiftKey && !event.metaKey && !event.ctrlKey && !event.isComposing) {
        event.preventDefault();
        void requestDraft();
      }
    },
  });
  const title = h("input", {
    class: "input title-input",
    maxlength: 100,
    placeholder: "비워 두면 설명의 첫 구절로 정해요",
    value: draft.title,
    onInput: (event: Event) => {
      patch({ title: (event.target as HTMLInputElement).value });
      syncCreate();
    },
  });
  const style = h("textarea", {
    class: "input mono-input",
    rows: 3,
    maxlength: 1500,
    placeholder: "melodic hip hop, male vocal, continuous drum groove, piano",
    value: draft.stylePrompt,
    onInput: (event: Event) => {
      patch({ stylePrompt: (event.target as HTMLTextAreaElement).value });
      syncCreate();
    },
  });
  const lyrics = h("textarea", {
    class: "input lyrics-input",
    rows: 16,
    maxlength: 4096,
    placeholder: "[Verse]\n새벽빛이 창가에 내려와\n조용했던 마음을 깨우네\n\n[Chorus]\n오늘의 우리를 기억해",
    value: draft.instrumental ? "" : draft.lyrics,
    onInput: (event: Event) => {
      const value = (event.target as HTMLTextAreaElement).value;
      startLyrics.value = value;
      patch({ lyrics: value });
      syncCreate();
    },
  });
  // Optional in step 1: lyrics written here are kept by the AI draft, which then only
  // fills the title and style. Both boxes edit the same draft lyrics.
  const startLyrics = h("textarea", {
    class: "input lyrics-input",
    rows: 8,
    maxlength: 4096,
    placeholder: "[Verse]\n가사를 붙여 넣으세요\n\n[Chorus]\n후렴 가사",
    value: draft.instrumental ? "" : draft.lyrics,
    onInput: (event: Event) => {
      const value = (event.target as HTMLTextAreaElement).value;
      lyrics.value = value;
      patch({ lyrics: value });
      syncCreate();
    },
  });
  const startLyricsBox = h("details", { class: "disclosure start-lyrics", open: Boolean(draft.lyrics.trim()) },
    h("summary", null, "가사가 이미 있다면 넣어 주세요 ", h("em", null, "선택 · 비워 두면 AI가 써요")),
    startLyrics,
    h("p", { class: "hint" }, "넣은 가사는 그대로 쓰고, AI 초안은 제목과 스타일만 채워요."),
  );
  const catalog = get().productionCatalog;
  const productionCheckboxes = new Map<string, HTMLInputElement>();
  const productionChoices = (catalog?.rules ?? []).map((rule) => {
    const checkbox = h("input", {
      type: "checkbox",
      onChange: (event: Event) => {
        patch({ productionRules: toggleProductionRule(get().create.productionRules ?? emptyProductionRules(), rule.id, (event.target as HTMLInputElement).checked) });
        syncCreate();
      },
    });
    productionCheckboxes.set(rule.id, checkbox);
    return h("label", { class: "rule-toggle", "data-tip": rule.description }, checkbox, h("span", null, rule.label));
  });
  const chips: HTMLButtonElement[] = [];
  const groups = STYLE_GROUPS.map((group) =>
    h(
      "div",
      { class: "tag-group" },
      h("span", { class: "tag-group-name" }, group.name),
      h(
        "div",
        { class: "chips" },
        group.tags.map(([label, tag]) => {
          const chip = h(
            "button",
            {
              type: "button",
              class: "chip small",
              "data-tip": `보내는 태그: ${tag}`,
              dataset: { tag },
              onClick: () => {
                const next = toggleTag(get().create.stylePrompt, tag);
                style.value = next;
                patch({ stylePrompt: next });
                syncCreate();
              },
            },
            label,
          );
          chips.push(chip);
          return chip;
        }),
      ),
    ),
  );
  const draftButton = h("button", { type: "button", class: "button primary", onClick: () => void requestDraft() }, icon("sparkle", 16), "AI 초안 쓰기");
  const startManual = h("button", {
    type: "button",
    class: "button ghost",
    onClick: () => {
      draftOpen = true;
      syncCreate();
      refs?.draftSection.scrollIntoView({ block: "start", behavior: "smooth" });
      refs?.title.focus({ preventScroll: true });
    },
  }, "직접 쓸게요");
  const bpm = h("input", { class: "input", inputmode: "numeric", placeholder: "예: 92", value: draft.bpm, onInput: (event: Event) => { patch({ bpm: (event.target as HTMLInputElement).value }); syncCreate(); } });
  const key = h("input", { class: "input", placeholder: "예: C major", value: draft.keyScale, onInput: (event: Event) => { patch({ keyScale: (event.target as HTMLInputElement).value }); syncCreate(); } });
  const meter = h(
    "select",
    { class: "input", onChange: (event: Event) => { patch({ timeSignature: (event.target as HTMLSelectElement).value }); syncCreate(); } },
    [["", "엔진에 맡기기"], ["4/4", "4/4 — 가장 흔한 박자"], ["3/4", "3/4 — 왈츠"], ["6/8", "6/8 — 흔들리는 느낌"]].map(([value, label]) =>
      h("option", { value, selected: draft.timeSignature === value }, label),
    ),
  );
  const songPlanButton = h("button", { type: "button", class: "button secondary small", onClick: () => void requestSongPlan() }, icon("lyrics", 14), "전개 미리보기");
  const createButton = h("button", { type: "button", class: "button primary large", onClick: (event: Event) => void submit(event.currentTarget as HTMLButtonElement) }, "곡 만들기");

  const r: Refs = {
    description,
    title,
    style,
    lyrics,
    startLyrics,
    startLyricsBox,
    lyricsField: h("div", { class: "lyrics-field" }),
    lyricsInstrumental: h("div", { class: "instrumental-note" }, icon("note", 18), h("div", null, h("b", null, "연주곡이에요"), h("p", null, "가사 없이 [Instrumental]로 보내요. 노래를 넣으려면 위에서 ‘한국어’나 ‘영어’를 고르세요."))),
    chips,
    voice: h("div", { class: "segmented", role: "group", "aria-label": "노래" }),
    instruments: h("div", { class: "chips", role: "group", "aria-label": "악기" }),
    styleHint: h("p", { class: "hint" }),
    durations: h("div", { class: "segmented", role: "group", "aria-label": "길이" }),
    versions: h("div", { class: "segmented", role: "group", "aria-label": "한 번에 만들 버전" }),
    draftButton,
    draftStatus: h("div", { class: "draft-status", role: "status" }),
    draftSection: h("section", { class: "draft", "aria-label": "초안" }),
    startManual,
    bpm,
    key,
    meter,
    lyricsHint: h("span", { class: "hint" }),
    presets: h("div", { class: "chips" }),
    presetHint: h("p", { class: "hint" }),
    productionCheckboxes,
    tuneSummary: h("em"),
    songPlanButton,
    songPlanStatus: h("div", { class: "song-plan-preview", role: "status" }),
    sendPreview: h("div", { class: "send-preview" }),
    bar: h("div", { class: "create-bar-text" }),
    createButton,
  };

  mount(
    r.lyricsField,
    h("div", { class: "field-head" },
      h("span", { class: "label" }, "가사"),
      h("div", { class: "lyrics-toolbar", role: "toolbar", "aria-label": "구간 태그 넣기" },
        SECTIONS.map(([tag, tip]) => h("button", { type: "button", class: "tag-button", "data-tip": tip, onClick: () => insertSection(tag) }, tag))),
    ),
    lyrics,
    r.lyricsHint,
  );

  mount(
    r.draftSection,
    h("header", { class: "step-head" },
      h("span", { class: "step-num" }, "2"),
      h("div", null, h("h2", null, "초안 다듬기"), h("p", null, "마음에 들지 않는 부분은 바로 고치세요. 그대로 만들어도 돼요.")),
      h("button", {
        type: "button",
        class: "icon-button step-close",
        "aria-label": "초안 다듬기 닫기",
        "data-tip": "닫기 · 쓴 내용은 남아 있어요",
        onClick: () => {
          draftOpen = false;
          syncCreate();
          refs?.description.focus();
        },
      }, icon("close", 16)),
    ),
    h("div", { class: "card draft-card" },
      h("label", { class: "field" }, h("span", { class: "label" }, "제목"), title),
      h("div", { class: "field" },
        h("div", { class: "field-head" },
          h("span", { class: "label" }, "스타일 ", h("em", null, "음악 엔진에 보내는 영어 프롬프트")),
          h("button", { type: "button", class: "button ghost small", onClick: async () => { await navigator.clipboard.writeText(style.value); toast("스타일을 복사했어요."); } }, icon("copy", 14), "복사"),
        ),
        style,
        r.styleHint,
        h("details", { class: "disclosure tag-picker" }, h("summary", null, "태그로 고르기 ", h("em", null, "누르면 넣고, 다시 누르면 빼요")), h("div", { class: "tag-groups" }, groups)),
      ),
      r.lyricsField,
      r.lyricsInstrumental,
    ),
    h("details", { class: "card tune" },
      h("summary", null, h("span", null, h("b", null, "세부 조정"), " ", r.tuneSummary), icon("chevron", 16)),
      h("div", { class: "tune-body" },
        h("div", { class: "field" }, h("span", { class: "label" }, "장르 추천 ", h("em", null, "고르면 어울리는 제작 규칙과 빠르기를 함께 켜요")), r.presets, r.presetHint),
        h("div", { class: "field" }, h("span", { class: "label" }, "제작 규칙 ", h("em", null, "켤수록 지시가 구체적이에요 · 올려 두면 설명이 보여요")), h("div", { class: "rule-grid" }, productionChoices)),
        h("div", { class: "field" }, h("span", { class: "label" }, "음악 정보 ", h("em", null, "모르면 비워 두세요")),
          h("div", { class: "field-row" },
            h("label", { class: "field" }, h("span", { class: "sublabel" }, "빠르기 (BPM)"), bpm),
            h("label", { class: "field" }, h("span", { class: "sublabel" }, "조성"), key),
            h("label", { class: "field" }, h("span", { class: "sublabel" }, "박자"), meter),
          ),
        ),
        h("div", { class: "field" },
          h("div", { class: "field-head" }, h("span", { class: "label" }, "곡 전개 ", h("em", null, "엔진을 켜지 않고 구간과 가사 호흡을 미리 봐요")), songPlanButton),
          r.songPlanStatus,
        ),
      ),
    ),
    h("details", { class: "card tune" },
      h("summary", null, h("span", null, h("b", null, "엔진에 보낼 내용"), " ", h("em", null, "실제로 보내는 프롬프트와 값")), icon("chevron", 16)),
      h("div", { class: "tune-body" }, r.sendPreview),
    ),
  );

  mount(
    root,
    h(
      "div",
      { class: "page narrow create-page" },
      h("header", { class: "page-head" }, h("div", null, h("h1", null, "새 곡 만들기"), h("p", null, "만들고 싶은 곡을 말로 적으면 AI가 제목·스타일·가사 초안을 써요."))),
      h(
        "section",
        { class: "composer", "aria-label": "어떤 곡을 원하세요" },
        h("header", { class: "step-head" }, h("span", { class: "step-num" }, "1"), h("div", null, h("h2", null, "어떤 곡을 원하세요?"))),
        h(
          "div",
          { class: "card composer-card" },
          description,
          h("div", { class: "examples" }, EXAMPLES.map((example) =>
            h("button", { type: "button", class: "example", onClick: () => { description.value = example; patch({ description: example }); description.focus(); } }, example))),
          r.startLyricsBox,
          h("div", { class: "option" }, h("span", { class: "sublabel" }, "악기 ", h("em", null, "선택 · 고르지 않으면 장르에 맞게 정해요")), r.instruments),
          h(
            "div",
            { class: "composer-options" },
            h("div", { class: "option" }, h("span", { class: "sublabel" }, "노래"), r.voice),
            h("div", { class: "option" }, h("span", { class: "sublabel" }, "길이"), r.durations),
            h("div", { class: "option" }, h("span", { class: "sublabel" }, "한 번에 만들 버전 ", h("span", { class: "help", "data-tip": "같은 설정으로 여러 번 만들어 마음에 드는 것을 골라요. 많을수록 오래 걸려요." }, "?")), r.versions),
          ),
          h("div", { class: "composer-actions" }, r.draftStatus, h("div", { class: "composer-buttons" }, startManual, draftButton)),
        ),
      ),
      r.draftSection,
      h("div", { class: "create-bar", hidden: !draftVisible(draft) }, r.bar, createButton),
    ),
  );
  refs = r;
}

function renderSendPreview(written: CreateDraft): void {
  if (!refs) return;
  const draft = sentDraft(written);
  const preview = productionPreview(draft, get().productionCatalog);
  const lines = draft.instrumental ? INSTRUMENTAL : draft.lyrics.trim();
  mount(
    refs.sendPreview,
    h("div", { class: "send-block" }, h("span", { class: "sublabel" }, "스타일 프롬프트 (제작 규칙 포함)"), h("p", { class: "mono caption-text" }, preview.stylePrompt || "아직 없음")),
    h(
      "dl",
      { class: "facts" },
      h("dt", null, "제목"), h("dd", null, draft.title.trim() || (draft.description ? fallbackTitle(draft.description) : "—")),
      h("dt", null, "길이"), h("dd", null, lengthLabel(draft.durationSeconds)),
      h("dt", null, "빠르기"), h("dd", null, preview.bpm ? `${preview.bpm} BPM` : "엔진에 맡기기"),
      h("dt", null, "조성"), h("dd", null, preview.keyScale || "엔진에 맡기기"),
      h("dt", null, "박자"), h("dd", null, preview.timeSignature || "엔진에 맡기기"),
      h("dt", null, "제작 규칙"), h("dd", null, preview.captions.length ? preview.captions.map((rule) => rule.label).join(", ") : "없음"),
    ),
    h("details", { class: "disclosure" }, h("summary", null, "가사"), h("pre", { class: "lyrics-view" }, lines || "비어 있음")),
    preview.warnings.map((warning) => h("p", { class: "notice tone-warn" }, warning)),
    draft.instrumental && (draft.productionRules?.ruleIds ?? []).some((id) => ["clear-vocal", "phrase-breathing"].includes(id)) && h("p", { class: "hint" }, "보컬과 가사 호흡 규칙은 연주곡에 보내지 않아요."),
  );
}

export function syncCreate(): void {
  if (!refs) return;
  const state = get();
  const draft = state.create;
  const selection = draft.productionRules ?? emptyProductionRules();
  const preview = productionPreview(draft, state.productionCatalog);
  const catalog = state.productionCatalog;

  const voice = voiceOf(draft);
  mount(refs.voice, segmented<Voice>(["ko", "en", "inst"], voice, (value) => ({ ko: "한국어", en: "영어", inst: "연주곡" })[value], (value) => {
    if (value === voiceOf(get().create)) return;
    if (value === "inst") patch({ instrumental: true });
    else patch({ instrumental: false, vocalLanguage: value });
    if (refs) refs.lyrics.value = refs.startLyrics.value = value === "inst" ? "" : get().create.lyrics;
    syncCreate();
  }, (value) => value === "inst" ? "가사 없이 연주만" : "AI 초안의 가사 언어예요. 직접 쓴 가사는 그대로 둬요"));
  mount(refs.durations, segmented(DURATIONS, draft.durationSeconds, (value) => (DURATIONS.includes(value) ? lengthLabel(value) : clock(value)), (value) => { patch({ durationSeconds: value }); syncCreate(); }));
  mount(refs.versions, segmented([1, 2, 3, 4], draft.versions, (value) => String(value), (value) => { patch({ versions: value }); syncCreate(); }));

  const visible = draftVisible(draft);
  mount(refs.instruments, INSTRUMENTS.map((instrument) => {
    const on = (draft.instruments ?? []).includes(instrument.tag) || (visible && hasInstrument(draft.stylePrompt, instrument));
    return h("button", {
      type: "button",
      class: `chip small${on ? " is-active" : ""}`,
      "aria-pressed": on ? "true" : "false",
      "data-tip": `보내는 태그: ${instrument.tag}`,
      onClick: () => {
        const current = get().create;
        const picked = new Set(current.instruments ?? []);
        if (on) picked.delete(instrument.tag);
        else picked.add(instrument.tag);
        const change: Partial<CreateDraft> = { instruments: [...picked] };
        // With the draft open the style prompt is the truth; keep it in step.
        if (draftVisible(current)) {
          change.stylePrompt = on ? removeInstrument(current.stylePrompt, instrument) : addInstrument(current.stylePrompt, instrument);
          if (refs) refs.style.value = change.stylePrompt;
        }
        patch(change);
        syncCreate();
      },
    }, instrument.label);
  }));
  const unwritten = pickedInstruments(draft).filter((instrument) => !hasInstrument(draft.stylePrompt, instrument));
  refs.styleHint.textContent = unwritten.length ? `위에서 고른 악기(${unwritten.map((item) => item.label).join(", ")})도 함께 보내요.` : "";
  refs.styleHint.hidden = !unwritten.length;
  refs.draftSection.hidden = !visible;
  // Step 1 has one job (the draft button); the create bar joins once there is a draft.
  refs.bar.parentElement!.hidden = !visible;
  refs.startManual.hidden = visible;
  refs.startManual.textContent = hasDraftContent(draft) ? "초안 다시 열기" : "직접 쓸게요";
  // withBusy owns the button's label and disabled state while a draft is being written.
  if (!drafting) refs.draftButton.replaceChildren(icon("sparkle", 16), visible && draft.drafted ? "초안 다시 쓰기" : "AI 초안 쓰기");
  if (!refs.draftStatus.childNodes.length && !drafting) {
    const llm = state.settings.assistant.kind !== "rules";
    mount(refs.draftStatus, h("span", { class: "hint" }, llm
      ? `로컬 LLM(${state.settings.assistant.model || "모델 미선택"})이 초안을 써요 · Enter`
      : ["기본 규칙으로 초안을 써요. 더 자유로운 작사는 ", h("button", { type: "button", class: "link", onClick: () => go("settings") }, "설정에서 로컬 LLM"), "을 켜세요."]));
  }

  // Once step 2 is open its lyrics box is the one place to edit them.
  refs.startLyricsBox.hidden = draft.instrumental || visible;
  refs.lyricsField.hidden = draft.instrumental;
  refs.lyricsInstrumental.hidden = !draft.instrumental;
  const lines = draft.lyrics.split("\n").filter((line) => line.trim() && !/^\[.*\]$/.test(line.trim())).length;
  const sections = (draft.lyrics.match(/^\s*\[[^\]]+\]\s*$/gm) ?? []).length;
  refs.lyricsHint.textContent = `${lines}줄 · 구간 ${sections}개 — [Verse], [Chorus]처럼 구간을 나누면 곡 구조가 안정돼요.`;

  for (const chip of refs.chips) {
    const on = hasTag(draft.stylePrompt, chip.dataset.tag ?? "");
    chip.classList.toggle("is-active", on);
    chip.setAttribute("aria-pressed", on ? "true" : "false");
  }

  mount(
    refs.presets,
    h("button", { type: "button", class: `chip${selection.presetId ? "" : " is-active"}`, "aria-pressed": selection.presetId ? "false" : "true", onClick: () => { patch({ productionRules: selectProductionPreset(null, get().productionCatalog) }); syncCreate(); } }, "고르지 않음"),
    (catalog?.presets ?? []).map((item) =>
      h("button", { type: "button", class: `chip${selection.presetId === item.id ? " is-active" : ""}`, "aria-pressed": selection.presetId === item.id ? "true" : "false", "data-tip": item.description, onClick: () => { patch({ productionRules: selectProductionPreset(item.id, get().productionCatalog) }); syncCreate(); } }, item.label)),
  );
  refs.presetHint.textContent = !catalog ? "제작 규칙 목록을 불러오지 못했어요." : preview.preset ? `${preview.preset.description} 직접 쓴 스타일·가사는 그대로 둬요.` : "";
  refs.presetHint.hidden = !refs.presetHint.textContent;
  for (const [id, checkbox] of refs.productionCheckboxes) checkbox.checked = selection.ruleIds.includes(id);
  refs.bpm.placeholder = preview.preset?.bpm ? `추천 ${preview.preset.bpm}` : "예: 92";
  refs.key.placeholder = preview.preset?.keyScale ? `추천 ${preview.preset.keyScale}` : "예: C major";
  const tuned = [preview.preset?.label, selection.ruleIds.length ? `규칙 ${selection.ruleIds.length}개` : null, draft.bpm.trim() ? `${draft.bpm.trim()} BPM` : null].filter(Boolean);
  refs.tuneSummary.textContent = tuned.length ? tuned.join(" · ") : "장르 추천 · 제작 규칙 · 빠르기 (선택)";

  refs.songPlanButton.disabled = planningSong || creating;
  let songPlan: SongPlanResult | null = null;
  try { songPlan = songPlanPreview.current(songPlanInput(draft)); } catch { /* Invalid typed metadata is reported on request. */ }
  mount(refs.songPlanStatus, songPlan ? songPlanView(songPlan) : planningSong ? h("p", { class: "hint" }, "전개와 호흡을 계산하는 중이에요.") : null);

  renderSendPreview(draft);

  const reason = blocker(draft);
  const ready = engineReady();
  refs.createButton.disabled = Boolean(reason) || creating || planningSong;
  if (!refs.createButton.classList.contains("is-busy")) refs.createButton.textContent = `곡 만들기 · 버전 ${draft.versions}개`;
  const facts = [lengthLabel(draft.durationSeconds), ({ ko: "한국어 노래", en: "영어 노래", inst: "연주곡" } as const)[voice], preview.preset?.label].filter(Boolean).join(" · ");
  mount(
    refs.bar,
    h("b", null, draft.title.trim() || (draft.description.trim() ? fallbackTitle(draft.description) : "새 곡")),
    h("span", { class: reason ? "create-bar-reason" : "hint" }, reason ? reason : `${facts} · ⌘ Enter`),
    !ready && ["offline", "failed"].includes(state.engine.state) && h("button", { type: "button", class: "button small secondary", onClick: () => void startEngine() }, icon("power", 14), "엔진 켜기"),
  );
}

export function renderCreate(): void {
  if (!refs || !refs.description.isConnected) build();
  syncCreate();
}

export function submitCreateShortcut(): void {
  if (refs && !refs.createButton.disabled) void submit(refs.createButton);
}

// Screenshot fixture only (?demo=draft|lyrics): fill the description (and lyrics), then ask for the real draft.
export function demoDraft(description: string, lyrics: string | null, draft: boolean): void {
  if (!refs) return;
  refs.description.value = description;
  patch({ description });
  if (lyrics) {
    refs.startLyrics.value = refs.lyrics.value = lyrics;
    refs.startLyricsBox.open = true;
    patch({ lyrics });
    syncCreate();
  }
  if (draft) void requestDraft();
}

export function resetCreateView(): void {
  refs = null;
}
