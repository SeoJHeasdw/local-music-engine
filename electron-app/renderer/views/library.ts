import type { SongSummary } from "../../shared.ts";
import { api, go, openFolderDialog, openSong, refreshSongs } from "../actions.ts";
import { byId, h, icon, mount } from "../dom.ts";
import { lengthLabel, relativeTime } from "../format.ts";
import { get } from "../store.ts";
import { errorText, toast } from "../ui.ts";

function reveal(songId: string) {
  void api.reveal({ kind: "song", songId }).catch((error) => toast(errorText(error), { tone: "error" }));
}

function statusPill(song: SongSummary) {
  if (song.running) return h("span", { class: "pill tone-accent" }, h("i", { class: "pulse-dot" }), "만드는 중");
  if (song.exported) return h("span", { class: "pill tone-ok" }, "내보냄");
  if (song.hasFinal) return h("span", { class: "pill tone-ok" }, "최종본 있음");
  if (song.versionCount && !song.reviewed) return h("span", { class: "pill" }, "들어 볼 차례");
  return null;
}

function card(song: SongSummary) {
  const open = get().song.song?.songId === song.songId;
  if (song.error) {
    return h(
      "article",
      { class: "song-card is-broken" },
      h("header", { class: "song-card-head" }, h("h3", null, song.title), h("span", { class: "pill tone-danger" }, "읽을 수 없음")),
      h("p", { class: "song-card-style" }, song.error),
      h("footer", { class: "song-card-foot" }, h("span", { class: "mono muted" }, song.folderName), h("button", { type: "button", class: "icon-button", "data-tip": "Finder에서 보기", "aria-label": "Finder에서 보기", onClick: () => reveal(song.songId) }, icon("folder", 16))),
    );
  }
  const facts = [
    `버전 ${song.versionCount - song.editCount}`,
    song.editCount ? `수정 ${song.editCount}` : null,
    song.liked ? `좋아요 ${song.liked}` : null,
    song.instrumental ? "연주곡" : null,
    lengthLabel(song.durationSeconds),
  ].filter(Boolean);
  return h(
    "article",
    { class: `song-card${open ? " is-open" : ""}` },
    h(
      "button",
      { type: "button", class: "song-card-main", onClick: () => void openSong(song.songId), "aria-label": `${song.title} 열기` },
      h("header", { class: "song-card-head" }, h("h3", null, song.title), statusPill(song)),
      h("p", { class: "song-card-style" }, song.stylePrompt || "스타일 없음"),
      h("p", { class: "song-card-facts" }, facts.join(" · ")),
    ),
    h(
      "footer",
      { class: "song-card-foot" },
      h("span", { class: "muted" }, relativeTime(song.updatedAt), song.external ? " · 다른 폴더" : ""),
      h("button", { type: "button", class: "icon-button", "data-tip": "Finder에서 곡 폴더 보기", "aria-label": "Finder에서 곡 폴더 보기", onClick: () => reveal(song.songId) }, icon("folder", 16)),
    ),
  );
}

export function renderLibrary(): void {
  const state = get();
  const root = byId("view-library");
  const songs = state.songs;
  mount(
    root,
    h(
      "header",
      { class: "view-head" },
      h("div", null, h("h1", null, "내 곡"), h("p", null, "곡마다 여러 버전을 만들고, 듣고, 고친 기록이 남아 있어요.")),
      h(
        "div",
        { class: "head-actions" },
        h("button", { type: "button", class: "button secondary", onClick: () => void openFolderDialog() }, icon("folder", 16), "다른 폴더의 곡 열기"),
        h("button", { type: "button", class: "button primary", onClick: () => go("create") }, icon("plus", 16), "새 곡 만들기"),
      ),
    ),
    songs.length
      ? h("div", { class: "song-grid" }, songs.map(card))
      : h(
          "div",
          { class: "empty" },
          h("span", { class: "empty-mark", "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("i"), h("i")),
          h("h2", null, "아직 만든 곡이 없어요"),
          h("p", null, "만들고 싶은 곡을 한 문장으로 설명하면, 음악 엔진이 스타일과 가사 초안을 먼저 써 줘요."),
          h("button", { type: "button", class: "button primary", onClick: () => go("create") }, icon("plus", 16), "첫 곡 만들기"),
        ),
    h(
      "footer",
      { class: "library-foot" },
      h("span", null, "저장 위치 "),
      h("span", { class: "mono" }, state.settings.projectsDir.replace(/^\/Users\/[^/]+/, "~")),
      h("button", { type: "button", class: "link", onClick: () => void api.reveal({ kind: "songs-dir" }) }, "Finder에서 열기"),
      h("button", { type: "button", class: "link", onClick: () => go("settings") }, "위치 바꾸기"),
      h("button", { type: "button", class: "link", onClick: () => void refreshSongs() }, "새로 고침"),
    ),
  );
}
