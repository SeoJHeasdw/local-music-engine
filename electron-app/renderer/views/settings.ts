import type { AssistantKind, Settings, Strength } from "../../shared.ts";
import { api, engineReady, refreshSongs } from "../actions.ts";
import { byId, h, icon, mount } from "../dom.ts";
import { elapsed, lengthLabel, strengthCopy } from "../format.ts";
import { get, set } from "../store.ts";
import { errorText, toast, withBusy } from "../ui.ts";

let models: string[] = [];
let modelsFor = "";

async function save(partial: Partial<Settings>, message = "설정을 저장했어요."): Promise<void> {
  try {
    set({ settings: await api.saveSettings(partial) });
    toast(message, { tone: "ok", timeout: 2200 });
  } catch (error) {
    toast(errorText(error), { tone: "error" });
    renderSettings();
  }
}

function section(title: string, description: string, ...body: Array<Node | null | false>) {
  return h(
    "section",
    { class: "settings-section" },
    h("div", { class: "settings-intro" }, h("h2", null, title), h("p", null, description)),
    h("div", { class: "settings-body card" }, body),
  );
}

function row(label: string, hint: string | null, control: Node) {
  return h("div", { class: "setting-row" }, h("div", { class: "setting-label" }, h("b", null, label), hint && h("p", null, hint)), h("div", { class: "setting-control" }, control));
}

function segmented<T extends string | number>(values: T[], current: T, label: (value: T) => string, pick: (value: T) => void, tip?: (value: T) => string) {
  return h(
    "div",
    { class: "segmented" },
    values.map((value) =>
      h("button", { type: "button", class: value === current ? "is-active" : "", "aria-pressed": value === current ? "true" : "false", "data-tip": tip?.(value), onClick: () => pick(value) }, label(value)),
    ),
  );
}

async function loadModels(kind: AssistantKind, baseUrl: string, button?: HTMLButtonElement): Promise<void> {
  // Mark first so a failing server is asked once per address, not on every re-render.
  modelsFor = `${kind}|${baseUrl}`;
  const result = await withBusy(button ?? null, "불러오는 중", () => api.listAssistantModels(kind, baseUrl));
  models = result ?? [];
  if (result && !result.length) toast("이 주소에서 모델을 찾지 못했어요.", { tone: "error" });
  if (get().view === "settings") renderSettings();
}

export function renderSettings(): void {
  const state = get();
  const settings = state.settings;
  const engine = state.engine;
  const root = byId("view-settings");
  const assistant = settings.assistant;

  const engineState: Record<string, string> = {
    checking: "확인 중",
    offline: "꺼져 있어요",
    starting: "켜는 중",
    ready: "켜져 있어요 · 앱이 켠 엔진",
    external: "켜져 있어요 · 앱 밖에서 켠 엔진",
    stopping: "끄는 중",
    failed: "멈췄어요",
    missing: "설치되지 않았어요",
  };

  const engineCard = h(
    "div",
    { class: `engine-status tone-${engineReady() ? "on" : engine.state === "starting" ? "busy" : "off"}` },
    h("span", { class: "engine-dot", "aria-hidden": "true" }),
    h(
      "div",
      { class: "engine-status-text" },
      h("b", null, engineState[engine.state] ?? engine.state),
      h("p", null, engine.state === "starting" ? `${engine.detail} · ${elapsed(engine.since)}` : engine.detail),
      engineReady() && h("p", { class: "mono" }, [engine.models.dit, engine.models.lm].filter(Boolean).join(" · "), engine.lmReady ? "" : " · 작사 모델 대기"),
    ),
    h(
      "div",
      { class: "engine-status-actions" },
      ["offline", "failed", "missing"].includes(engine.state) &&
        h("button", { type: "button", class: "button primary small", disabled: engine.state === "missing", onClick: (event: Event) => void withBusy(event.currentTarget as HTMLButtonElement, "켜는 중", async () => set({ engine: await api.startEngine() })) }, icon("power", 14), "켜기"),
      engine.owned &&
        ["ready", "starting"].includes(engine.state) &&
        h("button", { type: "button", class: "button secondary small", disabled: Boolean(state.task), "data-tip": state.task ? "만드는 중에는 끌 수 없어요" : "엔진을 끄면 메모리를 돌려받아요", onClick: (event: Event) => void withBusy(event.currentTarget as HTMLButtonElement, "끄는 중", async () => set({ engine: await api.stopEngine() })) }, "끄기"),
      h("button", { type: "button", class: "button ghost small", onClick: (event: Event) => void withBusy(event.currentTarget as HTMLButtonElement, "확인 중", async () => set({ engine: await api.checkEngine() })) }, icon("refresh", 14), "다시 확인"),
    ),
  );

  const autoStart = h("input", { type: "checkbox", checked: settings.aceAutoStart, onChange: (event: Event) => void save({ aceAutoStart: (event.target as HTMLInputElement).checked }) });
  const aceUrl = h("input", { class: "input mono-input", value: settings.aceBaseUrl, onChange: (event: Event) => void save({ aceBaseUrl: (event.target as HTMLInputElement).value }) });
  const log = engine.log.length
    ? h("details", { class: "disclosure" }, h("summary", null, "엔진 기록 최근 줄"), h("pre", { class: "log-view" }, engine.log.slice(-40).join("\n")))
    : null;

  // ---- assistant ----
  const kinds: Array<{ kind: AssistantKind; title: string; body: string }> = [
    { kind: "rules", title: "기본 규칙", body: `오프라인에서 바로 답해요. 수정 요청은 정해진 표현 ${state.rules.length}가지를 알아듣고, 초안은 음악 엔진이 스타일만 써요.` },
    { kind: "ollama", title: "로컬 LLM · Ollama", body: "자유로운 문장을 해석하고 한글 가사까지 써요. 이 Mac의 Ollama를 쓰고, 한 번에 수십 초 걸려요." },
    { kind: "openai", title: "OpenAI 호환 로컬 서버", body: "LM Studio처럼 /v1/chat/completions를 여는 로컬 서버를 써요." },
  ];
  const needsModels = assistant.kind !== "rules" && modelsFor !== `${assistant.kind}|${assistant.baseUrl}`;
  const assistantUrl = h("input", {
    class: "input mono-input",
    value: assistant.baseUrl,
    onChange: (event: Event) => void save({ assistant: { ...assistant, baseUrl: (event.target as HTMLInputElement).value } }),
  });
  const modelOptions = models.includes(assistant.model) || !assistant.model ? models : [assistant.model, ...models];
  const modelSelect = h(
    "select",
    { class: "input", onChange: (event: Event) => void save({ assistant: { ...assistant, model: (event.target as HTMLSelectElement).value } }) },
    h("option", { value: "", selected: !assistant.model }, modelOptions.length ? "모델을 고르세요" : "먼저 모델 목록을 불러오세요"),
    modelOptions.map((name) => h("option", { value: name, selected: name === assistant.model }, name)),
  );
  const assistantCards = h(
    "div",
    { class: "choice-cards", role: "radiogroup", "aria-label": "AI 도우미" },
    kinds.map((item) =>
      h(
        "button",
        {
          type: "button",
          role: "radio",
          "aria-checked": assistant.kind === item.kind ? "true" : "false",
          class: `choice-card${assistant.kind === item.kind ? " is-active" : ""}`,
          onClick: () => {
            if (assistant.kind === item.kind) return;
            const baseUrl = item.kind === "ollama" ? "http://127.0.0.1:11434" : item.kind === "openai" ? "http://127.0.0.1:1234" : assistant.baseUrl;
            void save({ assistant: { kind: item.kind, baseUrl, model: item.kind === assistant.kind ? assistant.model : "" } }, `AI 도우미를 ‘${item.title}’로 바꿨어요.`);
          },
        },
        h("b", null, item.title),
        h("p", null, item.body),
      ),
    ),
  );

  mount(
    root,
    h("header", { class: "view-head" }, h("div", null, h("h1", null, "설정"), h("p", null, "바꾸면 바로 저장돼요."))),
    h(
      "div",
      { class: "settings" },
      section(
        "음악 엔진",
        "ACE-Step 1.5가 이 Mac 안에서 곡을 만들어요. 모델이 메모리를 12GB쯤 써요.",
        engineCard,
        row("앱을 열 때 엔진 켜기", "끄면 곡을 만들 때 직접 켜야 해요.", h("label", { class: "switch" }, autoStart, h("span", null, settings.aceAutoStart ? "켬" : "끔"))),
        row("엔진 주소", "이 Mac 안의 주소만 쓸 수 있어요.", aceUrl),
        row("엔진 기록", "엔진이 멈췄을 때 원인을 찾을 수 있어요.", h("button", { type: "button", class: "button ghost small", onClick: () => void api.reveal({ kind: "engine-log" }).catch((error) => toast(errorText(error), { tone: "error" })) }, icon("folder", 14), "Finder에서 보기")),
        log,
      ),
      section(
        "AI 도우미",
        "새 곡의 초안(제목·스타일·가사)을 쓰고, ‘발음이 뭉개져요’ 같은 말을 엔진이 알아듣는 스타일 태그와 구간으로 바꿔요. 결과는 항상 실행 전에 보여 드려요.",
        assistantCards,
        assistant.kind !== "rules" && row("서버 주소", null, assistantUrl),
        assistant.kind !== "rules" &&
          row(
            "모델",
            "음악 엔진과 메모리를 나눠 써요. 도우미는 답한 뒤 모델을 바로 내려요.",
            h(
              "div",
              { class: "inline-controls" },
              modelSelect,
              h("button", { type: "button", class: "button secondary small", onClick: (event: Event) => void loadModels(assistant.kind, assistant.baseUrl, event.currentTarget as HTMLButtonElement) }, icon("refresh", 14), needsModels ? "목록 불러오기" : "다시 불러오기"),
            ),
          ),
      ),
      section(
        "곡 저장 위치",
        "새 곡은 이 폴더 안에 곡마다 폴더로 저장돼요. 원본 음원과 기록(project.json)이 함께 있어요.",
        row(
          "폴더",
          null,
          h(
            "div",
            { class: "inline-controls" },
            h("span", { class: "path mono" }, settings.projectsDir.replace(/^\/Users\/[^/]+/, "~")),
            h("button", { type: "button", class: "button secondary small", onClick: async () => { set({ settings: await api.pickProjectsDir() }); await refreshSongs(); } }, "바꾸기"),
            h("button", { type: "button", class: "button ghost small", onClick: () => void api.reveal({ kind: "songs-dir" }) }, icon("folder", 14), "Finder에서 열기"),
          ),
        ),
      ),
      section(
        "만들기 기본값",
        "새 곡과 ‘더 만들기’에 쓰는 값이에요. 곡마다 바꿀 수 있어요.",
        row("한 번에 만들 버전", null, segmented([1, 2, 3, 4], settings.defaultVersions, (value) => `${value}개`, (value) => void save({ defaultVersions: value }))),
        row("기본 길이", null, segmented([30, 60, 120, 180, 240], settings.defaultDurationSeconds, (value) => lengthLabel(value), (value) => void save({ defaultDurationSeconds: value }))),
        row(
          "구간 수정 변화 정도",
          "구간을 고칠 때 처음 고른 값이에요.",
          segmented(["light", "medium", "strong"] as Strength[], settings.repaintStrength, (value) => strengthCopy[value].label, (value) => {
            set({ strength: value });
            void save({ repaintStrength: value });
          }, (value) => strengthCopy[value].hint),
        ),
      ),
      section(
        "고급",
        "바꾼 모델은 엔진을 껐다 켜야 적용돼요. 처음 쓰는 모델은 켤 때 내려받아요.",
        row("곡 생성 모델", null, h("input", { class: "input mono-input", value: settings.ditModel, onChange: (event: Event) => void save({ ditModel: (event.target as HTMLInputElement).value }) })),
        row("작사·구조 모델", null, h("input", { class: "input mono-input", value: settings.lmModel, onChange: (event: Event) => void save({ lmModel: (event.target as HTMLInputElement).value }) })),
      ),
      section(
        "정보",
        "",
        row("앱 버전", null, h("span", { class: "mono" }, state.info.version)),
        row("엔진 코드", null, h("span", { class: "path mono" }, state.info.engineRoot.replace(/^\/Users\/[^/]+/, "~"))),
        row("앱 데이터", "설정과 엔진 기록이 있어요.", h("span", { class: "path mono" }, state.info.dataDir.replace(/^\/Users\/[^/]+/, "~"))),
      ),
    ),
  );
  if (assistant.kind !== "rules" && needsModels && !models.length) void loadModels(assistant.kind, assistant.baseUrl).catch(() => undefined);
}
