import type { SongSummary } from "../../shared.ts";
import { api, go, openFolderDialog, openSong, refreshSongs } from "../actions.ts";
import { byId, h, icon, mount } from "../dom.ts";
import { lengthLabel, relativeTime } from "../format.ts";
import { get } from "../store.ts";
import { errorText, toast } from "../ui.ts";

let query = "";
let list: HTMLElement | null = null;

function reveal(songId: string) {
  void api.reveal({ kind: "song", songId }).catch((error) => toast(errorText(error), { tone: "error" }));
}

function status(song: SongSummary) {
  if (song.error) return h("span", { class: "pill tone-danger" }, "읽을 수 없음");
  if (song.running || get().task?.songId === song.songId) return h("span", { class: "pill tone-accent" }, h("i", { class: "pulse-dot" }), "만드는 중");
  if (song.exported) return h("span", { class: "pill tone-ok" }, "내보냄");
  if (song.hasFinal) return h("span", { class: "pill tone-ok" }, "최종본 있음");
  if (song.versionCount && !song.reviewed) return h("span", { class: "pill tone-accent" }, "들어 볼 차례");
  if (!song.versionCount) return h("span", { class: "pill" }, "버전 없음");
  return h("span", { class: "pill" }, "듣는 중");
}

function row(song: SongSummary) {
  const open = get().song.song?.songId === song.songId;
  const versions = song.versionCount - song.editCount;
  return h(
    "li",
    { class: `song-row${open ? " is-open" : ""}${song.error ? " is-broken" : ""}` },
    h(
      "button",
      { type: "button", class: "song-row-main", disabled: Boolean(song.error), onClick: () => void openSong(song.songId), "aria-label": `${song.title} 열기` },
      h("span", { class: "song-row-art", "aria-hidden": "true" }, icon("note", 16)),
      h(
        "span",
        { class: "song-row-title" },
        h("b", null, song.title),
        h("small", null, song.error ?? (song.stylePrompt || "스타일 없음")),
      ),
      h("span", { class: "song-row-status" }, status(song)),
      h("span", { class: "song-row-num" }, song.error ? "—" : `${versions}${song.editCount ? ` + 수정 ${song.editCount}` : ""}`),
      h("span", { class: "song-row-num" }, song.instrumental ? `${lengthLabel(song.durationSeconds)} · 연주곡` : lengthLabel(song.durationSeconds)),
      h("span", { class: "song-row-time" }, relativeTime(song.updatedAt), song.external ? " · 다른 폴더" : ""),
    ),
    h("button", { type: "button", class: "icon-button", "data-tip": "Finder에서 곡 폴더 보기", "aria-label": "Finder에서 곡 폴더 보기", onClick: () => reveal(song.songId) }, icon("folder", 16)),
  );
}

function matches(song: SongSummary): boolean {
  const text = query.trim().toLowerCase();
  return !text || song.title.toLowerCase().includes(text) || song.stylePrompt.toLowerCase().includes(text);
}

function renderList(): void {
  if (!list) return;
  const songs = [...get().songs]
    .sort((a, b) => Date.parse(b.updatedAt ?? "") - Date.parse(a.updatedAt ?? "") || 0)
    .filter(matches);
  mount(
    list,
    songs.length
      ? h(
          "ol",
          { class: "song-table" },
          h(
            "li",
            { class: "song-table-head", "aria-hidden": "true" },
            h("span", null, "제목"),
            h("span", null, "상태"),
            h("span", null, "버전"),
            h("span", null, "길이"),
            h("span", null, "마지막 작업"),
          ),
          songs.map(row),
        )
      : h("p", { class: "library-none" }, `‘${query.trim()}’와 맞는 곡이 없어요.`),
  );
}

export function renderLibrary(): void {
  const state = get();
  const root = byId("view-library");
  const songs = state.songs;
  if (!songs.length) {
    list = null;
    mount(
      root,
      h(
        "div",
        { class: "empty" },
        h("span", { class: "empty-mark", "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("i"), h("i")),
        h("h1", null, "첫 곡을 만들어 볼까요?"),
        h("p", null, "만들고 싶은 곡을 한 문장으로 적으면 AI가 제목·스타일·가사 초안을 써 줘요."),
        h("div", { class: "head-actions" },
          h("button", { type: "button", class: "button primary large", onClick: () => go("create") }, icon("plus", 16), "새 곡 만들기"),
          h("button", { type: "button", class: "button ghost", onClick: () => void openFolderDialog() }, icon("folder", 16), "다른 폴더의 곡 열기"),
        ),
      ),
    );
    return;
  }
  const search = h("input", {
    class: "input search-input",
    type: "search",
    placeholder: "제목이나 스타일로 찾기",
    value: query,
    "aria-label": "곡 찾기",
    onInput: (event: Event) => {
      query = (event.target as HTMLInputElement).value;
      renderList();
    },
  });
  const searching = document.activeElement?.classList.contains("search-input") ?? false;
  list = h("div", { class: "library-list" });
  mount(
    root,
    h(
      "div",
      { class: "page" },
      h(
        "header",
        { class: "page-head" },
        h("div", null, h("h1", null, "내 곡"), h("p", null, `${songs.length}곡 · 곡을 누르면 버전을 듣고 고칠 수 있어요`)),
        h(
          "div",
          { class: "head-actions" },
          h("button", { type: "button", class: "button ghost", onClick: () => void openFolderDialog() }, icon("folder", 16), "다른 폴더의 곡 열기"),
          h("button", { type: "button", class: "button primary", onClick: () => go("create") }, icon("plus", 16), "새 곡 만들기"),
        ),
      ),
      h("label", { class: "search" }, icon("search", 16), search),
      list,
      h(
        "footer",
        { class: "library-foot" },
        h("span", { class: "mono" }, state.settings.projectsDir.replace(/^\/Users\/[^/]+/, "~")),
        h("button", { type: "button", class: "link", onClick: () => void api.reveal({ kind: "songs-dir" }) }, "Finder에서 열기"),
        h("button", { type: "button", class: "link", onClick: () => go("settings") }, "위치 바꾸기"),
        h("button", { type: "button", class: "link", onClick: () => void refreshSongs() }, "새로 고침"),
      ),
    ),
  );
  renderList();
  // A refreshed song list must not steal the search box from someone typing in it.
  if (searching) {
    search.focus();
    search.setSelectionRange(search.value.length, search.value.length);
  }
}
