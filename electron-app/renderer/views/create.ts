import { api, applySong, engineReady, go, startEngine } from "../actions.ts";
import { byId, h, icon, mount } from "../dom.ts";
import { clock, hasTag, lengthLabel, splitTags, toggleTag } from "../format.ts";
import { get, set, type CreateDraft } from "../store.ts";
import { toast, withBusy } from "../ui.ts";

// Plain-Korean labels for the English tags ACE-Step was trained on. The chip shows the
// word a non-musician would use; the tooltip shows the exact tag that is sent.
const STYLE_GROUPS: Array<{ name: string; tags: Array<[string, string]> }> = [
  {
    name: "장르",
    tags: [
      ["발라드", "Korean ballad"], ["인디 팝", "indie pop"], ["K-팝 댄스", "K-pop dance"], ["시티팝", "city pop"],
      ["어쿠스틱 포크", "acoustic folk"], ["R&B", "R&B"], ["밴드 록", "band rock"], ["로파이", "lo-fi hip hop"],
      ["재즈", "jazz"], ["일렉트로닉", "electronic"], ["영화 음악", "cinematic orchestral"],
    ],
  },
  {
    name: "분위기",
    tags: [
      ["잔잔한", "calm"], ["따뜻한", "warm"], ["몽환적인", "dreamy"], ["신나는", "energetic"], ["희망찬", "uplifting"],
      ["애절한", "melancholic"], ["설레는", "romantic"], ["웅장한", "epic"], ["그리운", "nostalgic"],
    ],
  },
  {
    name: "목소리",
    tags: [
      ["여성 보컬", "female vocal"], ["남성 보컬", "male vocal"], ["속삭이듯", "breathy vocal"], ["힘 있게", "powerful vocal"],
      ["또렷한 발음", "clear Korean diction"], ["화음 코러스", "vocal harmonies"],
    ],
  },
  {
    name: "악기",
    tags: [
      ["피아노", "piano"], ["어쿠스틱 기타", "acoustic guitar"], ["일렉 기타", "electric guitar"], ["현악기", "strings"],
      ["신스", "synth pads"], ["베이스", "groovy bassline"], ["드럼 약하게", "light drums"], ["드럼 강하게", "punchy drums"],
    ],
  },
  {
    name: "속도와 질감",
    tags: [
      ["느리게", "slow tempo"], ["중간 빠르기", "mid-tempo"], ["빠르게", "upbeat"], ["단출하게", "minimal arrangement"],
      ["풍성하게", "lush arrangement"], ["깔끔한 믹스", "clean mix"], ["빈티지", "vintage warmth"],
    ],
  },
];

const EXAMPLES = [
  "비 오는 밤 혼자 걷는 느낌의 잔잔한 발라드, 여자 목소리, 피아노 중심",
  "여름 밤 드라이브에 어울리는 신나는 시티팝, 남자 목소리",
  "카페에서 흘러나오는 따뜻한 어쿠스틱 연주곡",
  "졸업식에서 부르는 희망찬 밴드 록, 떼창하기 좋은 후렴",
];

const SECTIONS: Array<[string, string]> = [
  ["[Intro]", "전주 — 노래가 시작되기 전 부분"],
  ["[Verse]", "벌스 — 이야기를 풀어 가는 부분. 보통 차분해요"],
  ["[Pre-Chorus]", "프리코러스 — 후렴 직전에 분위기를 끌어올리는 부분"],
  ["[Chorus]", "후렴 — 가장 기억에 남는, 반복되는 부분"],
  ["[Bridge]", "브리지 — 후반에 한 번 분위기를 바꾸는 부분"],
  ["[Outro]", "아웃트로 — 곡을 마무리하는 부분"],
];

const DURATIONS = [30, 60, 120, 180, 240];
const INSTRUMENTAL = "[Instrumental]";

type Refs = {
  description: HTMLTextAreaElement;
  title: HTMLInputElement;
  style: HTMLTextAreaElement;
  lyrics: HTMLTextAreaElement;
  chips: HTMLButtonElement[];
  durations: HTMLElement;
  versions: HTMLElement;
  summary: HTMLElement;
  draftButton: HTMLButtonElement;
  draftStatus: HTMLElement;
  instrumental: HTMLInputElement;
  bpm: HTMLInputElement;
  key: HTMLInputElement;
  meter: HTMLSelectElement;
  meta: HTMLDetailsElement;
  lyricsHint: HTMLElement;
};

let refs: Refs | null = null;

export function blankDraft(): CreateDraft {
  const settings = get()?.settings;
  return {
    description: "",
    instrumental: false,
    title: "",
    stylePrompt: "",
    lyrics: "",
    durationSeconds: settings?.defaultDurationSeconds ?? 120,
    versions: settings?.defaultVersions ?? 2,
    bpm: "",
    keyScale: "",
    timeSignature: "",
    drafted: false,
  };
}

function patch(change: Partial<CreateDraft>): void {
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
  patch({ lyrics: area.value });
}

async function requestDraft(): Promise<void> {
  if (!refs) return;
  const draft = get().create;
  if (!draft.description.trim()) {
    toast("어떤 곡인지 한 줄이라도 적어 주세요.", { tone: "error" });
    refs.description.focus();
    return;
  }
  const llm = get().settings.assistant.kind !== "rules";
  refs.draftStatus.textContent = llm
    ? "로컬 LLM이 제목·스타일·가사를 쓰고 있어요. 모델을 불러오느라 30초쯤 걸릴 수 있어요."
    : "음악 엔진이 스타일을 쓰고 있어요.";
  const result = await withBusy(refs.draftButton, "초안을 쓰는 중", () => api.draft(draft.description, draft.instrumental, draft.durationSeconds));
  if (!refs) return;
  refs.draftStatus.replaceChildren();
  if (!result) return;
  // Keep what the person already wrote; a draft only fills empty or drafted fields.
  const keepLyrics = !draft.drafted && draft.lyrics.trim() && !draft.instrumental;
  const lyrics = draft.instrumental ? INSTRUMENTAL : keepLyrics ? draft.lyrics : result.lyrics;
  const title = draft.title.trim() && !draft.drafted ? draft.title : result.title || draft.title || fallbackTitle(draft.description);
  patch({ stylePrompt: result.stylePrompt, lyrics, title, drafted: true });
  refs.style.value = result.stylePrompt;
  if (!draft.instrumental) refs.lyrics.value = lyrics;
  refs.title.value = title;
  flash(refs.style);
  if (!keepLyrics) flash(refs.lyrics);
  syncCreate();
  const source = result.source === "llm" ? `로컬 LLM(${result.sourceModel})이 쓴 초안이에요.` : "음악 엔진이 쓴 초안이에요.";
  mount(
    refs.draftStatus,
    h("span", { class: "draft-source" }, icon("sparkle", 14), source, " 마음에 들지 않는 부분은 바로 고치세요."),
    result.notes.map((note) => h("span", { class: "draft-note" }, icon("info", 14), note)),
  );
  toast("초안을 채웠어요.", { tone: "ok" });
}

async function submit(button: HTMLButtonElement): Promise<void> {
  const draft = get().create;
  const lyrics = draft.instrumental ? INSTRUMENTAL : draft.lyrics.trim();
  if (!draft.stylePrompt.trim()) {
    toast("스타일을 한 줄 적거나 아래 칩에서 골라 주세요.", { tone: "error" });
    refs?.style.focus();
    return;
  }
  if (!lyrics) {
    toast("가사를 적거나 ‘연주곡’을 켜 주세요.", { tone: "error" });
    refs?.lyrics.focus();
    return;
  }
  const bpm = Number.parseInt(draft.bpm, 10);
  const state = await withBusy(button, "만드는 중", () =>
    api.createSong({
      title: draft.title.trim() || fallbackTitle(draft.description),
      stylePrompt: draft.stylePrompt.trim(),
      lyrics,
      durationSeconds: draft.durationSeconds,
      versions: draft.versions,
      bpm: Number.isFinite(bpm) && bpm >= 30 && bpm <= 300 ? bpm : null,
      keyScale: draft.keyScale.trim() || null,
      timeSignature: draft.timeSignature.trim() || null,
    }),
  );
  if (!state) return;
  applySong(state);
  set({ create: blankDraft() });
  refs = null;
  go("studio");
  toast(`버전 ${draft.versions}개를 만들기 시작했어요. 끝나는 대로 하나씩 들어 볼 수 있어요.`, { tone: "ok" });
}

function segmented<T extends number>(values: T[], current: T, label: (value: T) => string, onPick: (value: T) => void) {
  const all = values.includes(current) ? values : [...values, current];
  return all.map((value) =>
    h(
      "button",
      {
        type: "button",
        class: value === current ? "is-active" : "",
        "aria-pressed": value === current ? "true" : "false",
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
    class: "input",
    rows: 3,
    maxlength: 1000,
    placeholder: "예: 비 오는 밤 혼자 걷는 느낌의 잔잔한 발라드. 여자 목소리, 피아노 중심.",
    value: draft.description,
    onInput: (event: Event) => patch({ description: (event.target as HTMLTextAreaElement).value }),
  });
  const title = h("input", {
    class: "input",
    maxlength: 100,
    placeholder: "비워 두면 설명의 첫 구절로 정해요",
    value: draft.title,
    onInput: (event: Event) => patch({ title: (event.target as HTMLInputElement).value }),
  });
  const style = h("textarea", {
    class: "input mono-input",
    rows: 3,
    maxlength: 1500,
    placeholder: "Korean ballad, calm, female vocal, piano",
    value: draft.stylePrompt,
    onInput: (event: Event) => {
      patch({ stylePrompt: (event.target as HTMLTextAreaElement).value });
      syncCreate();
    },
  });
  const lyrics = h("textarea", {
    class: "input lyrics-input",
    rows: 14,
    maxlength: 4096,
    placeholder: "[Verse]\n새벽빛이 창가에 내려와\n조용했던 마음을 깨우네\n\n[Chorus]\n오늘의 우리를 기억해",
    value: draft.instrumental ? "" : draft.lyrics,
    disabled: draft.instrumental,
    onInput: (event: Event) => {
      patch({ lyrics: (event.target as HTMLTextAreaElement).value });
      syncCreate();
    },
  });
  const instrumental = h("input", {
    type: "checkbox",
    checked: draft.instrumental,
    onChange: (event: Event) => {
      const on = (event.target as HTMLInputElement).checked;
      patch({ instrumental: on });
      lyrics.disabled = on;
      lyrics.value = on ? "" : get().create.lyrics;
      syncCreate();
    },
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
              class: "chip",
              "data-tip": tag,
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
  const draftButton = h(
    "button",
    { type: "button", class: "button secondary", onClick: () => void requestDraft() },
    icon("sparkle", 16),
    "스타일·가사 초안 받기",
  );
  const draftStatus = h("div", { class: "draft-status", role: "status" });
  const durations = h("div", { class: "segmented" });
  const versions = h("div", { class: "segmented" });
  const summary = h("div", { class: "create-summary-body" });
  const bpm = h("input", { class: "input", inputmode: "numeric", placeholder: "예: 92", value: draft.bpm, onInput: (event: Event) => patch({ bpm: (event.target as HTMLInputElement).value }) });
  const key = h("input", { class: "input", placeholder: "예: C major", value: draft.keyScale, onInput: (event: Event) => patch({ keyScale: (event.target as HTMLInputElement).value }) });
  const meter = h(
    "select",
    { class: "input", onChange: (event: Event) => patch({ timeSignature: (event.target as HTMLSelectElement).value }) },
    [["", "엔진에 맡기기"], ["4/4", "4/4 — 가장 흔한 박자"], ["3/4", "3/4 — 왈츠"], ["6/8", "6/8 — 흔들리는 느낌"]].map(([value, label]) =>
      h("option", { value, selected: draft.timeSignature === value }, label),
    ),
  );
  const meta = h(
    "details",
    { class: "disclosure" },
    h("summary", null, "음악 정보 ", h("em", null, "선택 · 모르면 비워 두세요")),
    h(
      "div",
      { class: "field-row" },
      h("label", { class: "field" }, h("span", { class: "label" }, "빠르기 (BPM)"), bpm),
      h("label", { class: "field" }, h("span", { class: "label" }, "조성"), key),
      h("label", { class: "field" }, h("span", { class: "label" }, "박자"), meter),
    ),
    h("p", { class: "hint" }, "비워 두면 음악 엔진이 스타일에 맞춰 정해요. 초안을 받으면 엔진이 제안한 값이 들어가요."),
  );
  const lyricsHint = h("p", { class: "hint" });

  mount(
    root,
    h(
      "header",
      { class: "view-head" },
      h("div", null, h("h1", null, "새 곡 만들기"), h("p", null, "원하는 느낌을 말로 설명하면 초안이 채워져요. 음악 용어는 몰라도 괜찮아요.")),
    ),
    h(
      "div",
      { class: "create-grid" },
      h(
        "form",
        { class: "create-form", onSubmit: (event: Event) => event.preventDefault() },
        h(
          "section",
          { class: "create-section is-lead" },
          h("h2", { class: "section-title" }, "어떤 곡을 원하세요?"),
          description,
          h(
            "div",
            { class: "example-row" },
            EXAMPLES.map((example) =>
              h(
                "button",
                {
                  type: "button",
                  class: "example",
                  onClick: () => {
                    description.value = example;
                    patch({ description: example });
                  },
                },
                example,
              ),
            ),
          ),
          h(
            "div",
            { class: "draft-row" },
            h("label", { class: "switch" }, instrumental, h("span", null, "연주곡 (가사 없음)")),
            draftButton,
          ),
          draftStatus,
        ),
        h(
          "section",
          { class: "create-section" },
          h("label", { class: "field" }, h("span", { class: "label" }, "제목"), title),
          h(
            "div",
            { class: "field" },
            h(
              "span",
              { class: "label" },
              "스타일 ",
              h("em", null, "음악 엔진이 읽는 영어 태그"),
              h("span", { class: "help", "data-tip": "음악 엔진은 쉼표로 나눈 영어 태그를 읽어요. 아래 칩을 누르면 태그가 들어가고, 다시 누르면 빠져요. 직접 적어도 돼요." }, "?"),
            ),
            style,
          ),
          h("div", { class: "tag-groups" }, groups),
        ),
        h(
          "section",
          { class: "create-section" },
          h(
            "div",
            { class: "field" },
            h("span", { class: "label" }, "가사"),
            h(
              "div",
              { class: "lyrics-toolbar", role: "toolbar", "aria-label": "구간 태그 넣기" },
              SECTIONS.map(([tag, tip]) =>
                h("button", { type: "button", class: "tag-button", "data-tip": tip, onClick: () => insertSection(tag) }, tag),
              ),
            ),
            lyrics,
            lyricsHint,
          ),
        ),
        h(
          "section",
          { class: "create-section" },
          h("div", { class: "field" }, h("span", { class: "label" }, "길이"), durations),
          h(
            "div",
            { class: "field" },
            h("span", { class: "label" }, "한 번에 만들 버전 ", h("span", { class: "help", "data-tip": "같은 설정으로 여러 번 만들어 그중 마음에 드는 것을 골라요. 많을수록 오래 걸려요." }, "?")),
            versions,
          ),
          meta,
        ),
      ),
      h("aside", { class: "create-summary" }, h("h2", { class: "section-title" }, "보낼 내용"), summary),
    ),
  );
  refs = { description, title, style, lyrics, chips, durations, versions, summary, draftButton, draftStatus, instrumental, bpm, key, meter, meta, lyricsHint };
}

export function syncCreate(): void {
  if (!refs) return;
  const state = get();
  const draft = state.create;
  for (const chip of refs.chips) {
    const on = hasTag(draft.stylePrompt, chip.dataset.tag ?? "");
    chip.classList.toggle("is-active", on);
    chip.setAttribute("aria-pressed", on ? "true" : "false");
  }
  mount(
    refs.durations,
    segmented(DURATIONS, draft.durationSeconds, (value) => (DURATIONS.includes(value) ? lengthLabel(value) : clock(value)), (value) => {
      patch({ durationSeconds: value });
      syncCreate();
    }),
  );
  mount(
    refs.versions,
    segmented([1, 2, 3, 4], draft.versions, (value) => `${value}개`, (value) => {
      patch({ versions: value });
      syncCreate();
    }),
  );
  const lines = draft.lyrics.split("\n").filter((line) => line.trim() && !/^\[.*\]$/.test(line.trim())).length;
  const sections = (draft.lyrics.match(/^\s*\[[^\]]+\]\s*$/gm) ?? []).length;
  refs.lyricsHint.textContent = draft.instrumental
    ? "연주곡은 가사 대신 [Instrumental]을 보내요."
    : `가사 ${lines}줄 · 구간 태그 ${sections}개. [Verse], [Chorus]처럼 구간을 나누면 곡 구조가 안정돼요.`;

  const ready = engineReady();
  const tags = splitTags(draft.stylePrompt);
  const createButton = h(
    "button",
    { type: "button", class: "button primary block large", disabled: !ready, onClick: (event: Event) => void submit(event.currentTarget as HTMLButtonElement) },
    `곡 만들기 · 버전 ${draft.versions}개`,
  );
  mount(
    refs.summary,
    h(
      "dl",
      { class: "facts" },
      h("dt", null, "제목"), h("dd", null, draft.title.trim() || (draft.description ? fallbackTitle(draft.description) : "—")),
      h("dt", null, "길이"), h("dd", null, lengthLabel(draft.durationSeconds)),
      h("dt", null, "버전"), h("dd", null, `${draft.versions}개`),
      h("dt", null, "가사"), h("dd", null, draft.instrumental ? "연주곡" : draft.lyrics.trim() ? `${lines}줄` : "비어 있음"),
    ),
    h("div", { class: "summary-caption" }, h("span", { class: "label" }, `스타일 태그 ${tags.length}개`), h("p", { class: "mono caption-text" }, draft.stylePrompt.trim() || "아직 없음")),
    !ready &&
      h(
        "div",
        { class: "notice tone-warn" },
        h("p", null, state.engine.state === "starting" ? "음악 엔진을 켜는 중이에요. 준비되면 만들 수 있어요." : "음악 엔진이 꺼져 있어요. 켜야 초안과 곡을 만들 수 있어요."),
        ["offline", "failed"].includes(state.engine.state) &&
          h("button", { type: "button", class: "button small secondary", onClick: () => void startEngine() }, icon("power", 14), "엔진 켜기"),
      ),
    createButton,
    h("p", { class: "hint center" }, "⌘ Enter로도 시작해요. 새 곡은 ", h("span", { class: "mono" }, state.settings.projectsDir.replace(/^\/Users\/[^/]+/, "~")), "에 저장돼요."),
  );
  const llm = state.settings.assistant.kind !== "rules";
  refs.draftButton.disabled = !ready && !llm;
  refs.draftButton.dataset.tip = ready || llm ? "" : "음악 엔진을 켜거나 설정에서 로컬 LLM 도우미를 켜세요";
  if (!refs.draftStatus.childNodes.length && !llm) {
    mount(
      refs.draftStatus,
      h(
        "span",
        { class: "draft-note" },
        icon("info", 14),
        "지금은 음악 엔진이 스타일만 제안해요. 한글 가사까지 받으려면 ",
        h("button", { type: "button", class: "link", onClick: () => go("settings") }, "설정에서 로컬 LLM 도우미"),
        "를 켜세요.",
      ),
    );
  }
}

export function renderCreate(): void {
  if (!refs || !refs.description.isConnected) build();
  syncCreate();
}

export function submitCreateShortcut(): void {
  const button = refs?.summary.querySelector<HTMLButtonElement>("button.primary");
  if (button && !button.disabled) void submit(button);
}

export function resetCreateView(): void {
  refs = null;
}

