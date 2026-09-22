import type { Candidate, ProjectState, Review } from "./shared.ts";

const byId = <T extends HTMLElement>(id: string): T => {
  const element = document.getElementById(id);
  if (!element) throw new Error(`missing element: ${id}`);
  return element as T;
};

const elements = {
  empty: byId<HTMLElement>("empty-state"),
  workspace: byId<HTMLElement>("workspace"),
  projectPath: byId<HTMLElement>("project-path"),
  saveState: byId<HTMLElement>("save-state"),
  candidateList: byId<HTMLElement>("candidate-list"),
  candidateCount: byId<HTMLElement>("candidate-count"),
  reviewedCount: byId<HTMLElement>("reviewed-count"),
  activeCandidate: byId<HTMLElement>("active-candidate"),
  activeBadge: byId<HTMLElement>("active-badge"),
  artifactState: byId<HTMLElement>("artifact-state"),
  audio: byId<HTMLAudioElement>("audio"),
  waveform: byId<HTMLCanvasElement>("waveform"),
  currentTime: byId<HTMLElement>("current-time"),
  duration: byId<HTMLElement>("duration"),
  editRange: byId<HTMLElement>("edit-range"),
  findings: byId<HTMLElement>("findings"),
  findingSummary: byId<HTMLElement>("finding-summary"),
  reviewState: byId<HTMLElement>("review-state"),
  reviewStatus: byId<HTMLSelectElement>("review-status"),
  reviewRating: byId<HTMLSelectElement>("review-rating"),
  reviewNote: byId<HTMLTextAreaElement>("review-note"),
  reviewNotes: byId<HTMLElement>("review-notes"),
  selectCandidate: byId<HTMLButtonElement>("select-candidate"),
  toast: byId<HTMLElement>("toast"),
};

let state: ProjectState = {
  status: "empty",
  projectPath: null,
  projectId: null,
  selectedCandidateId: null,
  candidates: [],
};
let activeCandidateId: string | null = null;
let waveformSamples: Float32Array | null = null;

function activeCandidate(): Candidate | null {
  return state.candidates.find((candidate) => candidate.candidateId === activeCandidateId) ?? null;
}

function shortId(id: string): string {
  return id.replace("candidate_", "").slice(0, 8);
}

function timeLabel(seconds: number): string {
  if (!Number.isFinite(seconds)) return "0:00.0";
  const minutes = Math.floor(seconds / 60);
  const remainder = (seconds % 60).toFixed(1).padStart(4, "0");
  return `${minutes}:${remainder}`;
}

function reviewLabel(status: Review["status"]): string {
  return {
    unreviewed: "미청취",
    listened: "청취함",
    approved: "승인",
    rejected: "거절",
  }[status];
}

function toast(message: string, error = false): void {
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", error);
  elements.toast.classList.remove("hidden");
  window.setTimeout(() => elements.toast.classList.add("hidden"), 2600);
}

async function action<T>(label: string, callback: () => Promise<T>): Promise<T | null> {
  elements.saveState.textContent = label;
  try {
    const result = await callback();
    elements.saveState.textContent = "저장됨";
    return result;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    elements.saveState.textContent = "오류";
    toast(message, true);
    return null;
  }
}

function renderCandidateList(): void {
  elements.candidateList.replaceChildren();
  for (const candidate of state.candidates) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "candidate-card";
    button.classList.toggle("active", candidate.candidateId === activeCandidateId);
    button.classList.toggle("selected", candidate.selected);
    button.setAttribute("role", "listitem");

    const name = document.createElement("strong");
    name.textContent = `${candidate.taskType === "repaint" ? "Repaint" : "Candidate"} · ${shortId(candidate.candidateId)}`;
    const meta = document.createElement("div");
    meta.className = "candidate-meta";
    for (const text of [
      `seed ${candidate.seed}`,
      timeLabel(candidate.durationSeconds),
      reviewLabel(candidate.humanReview.status),
      candidate.editRange
        ? `${candidate.editRange.startSeconds.toFixed(1)}–${candidate.editRange.endSeconds.toFixed(1)}s`
        : "전체",
    ]) {
      const span = document.createElement("span");
      span.textContent = text;
      meta.append(span);
    }
    button.append(name, meta);
    button.addEventListener("click", () => void activateCandidate(candidate.candidateId));
    elements.candidateList.append(button);
  }
}

function renderFindings(candidate: Candidate): void {
  elements.findings.replaceChildren();
  const warnings = candidate.findings.filter((finding) => finding.severity !== "info");
  elements.findingSummary.textContent = `${warnings.length} 경고`;
  for (const finding of candidate.findings) {
    const row = document.createElement("div");
    row.className = `finding ${finding.severity}`;
    const title = document.createElement("strong");
    title.textContent = `${finding.check} · ${finding.severity}`;
    const message = document.createElement("p");
    message.textContent = finding.message;
    row.append(title, message);
    elements.findings.append(row);
  }
}

function renderReview(candidate: Candidate): void {
  const review = candidate.humanReview;
  elements.reviewState.textContent = reviewLabel(review.status);
  elements.reviewStatus.value = review.status;
  elements.reviewRating.value = review.rating ? String(review.rating) : "";
  elements.reviewNote.value = "";
  elements.reviewNotes.replaceChildren();
  for (const note of [...review.notes].reverse()) {
    const row = document.createElement("div");
    row.className = "review-note";
    const time = document.createElement("time");
    time.textContent = new Date(note.createdAt).toLocaleString("ko-KR");
    const text = document.createElement("span");
    text.textContent = note.text;
    row.append(time, text);
    elements.reviewNotes.append(row);
  }
}

function drawWaveform(): void {
  const canvas = elements.waveform;
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(rect.width * ratio));
  const height = Math.max(1, Math.round(rect.height * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const context = canvas.getContext("2d");
  if (!context) return;
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#0a0d13";
  context.fillRect(0, 0, width, height);
  context.strokeStyle = "#252d3c";
  context.beginPath();
  context.moveTo(0, height / 2);
  context.lineTo(width, height / 2);
  context.stroke();
  if (!waveformSamples?.length) return;

  const step = Math.max(1, Math.floor(waveformSamples.length / width));
  context.strokeStyle = "#b8f54b";
  context.lineWidth = Math.max(1, ratio);
  context.beginPath();
  for (let x = 0; x < width; x += 1) {
    let peak = 0;
    const start = x * step;
    const end = Math.min(waveformSamples.length, start + step);
    for (let index = start; index < end; index += 1) {
      peak = Math.max(peak, Math.abs(waveformSamples[index]));
    }
    const y = peak * height * 0.44;
    context.moveTo(x, height / 2 - y);
    context.lineTo(x, height / 2 + y);
  }
  context.stroke();

  const duration = elements.audio.duration;
  if (Number.isFinite(duration) && duration > 0) {
    const progress = elements.audio.currentTime / duration;
    context.fillStyle = "rgba(126, 166, 255, 0.7)";
    context.fillRect(Math.round(width * progress), 0, Math.max(1, ratio), height);
  }
}

async function activateCandidate(candidateId: string, preserveTime = true): Promise<void> {
  const candidate = state.candidates.find((item) => item.candidateId === candidateId);
  if (!candidate) return;
  const previousTime = preserveTime ? elements.audio.currentTime : 0;
  const wasPlaying = !elements.audio.paused;
  activeCandidateId = candidateId;
  renderCandidateList();
  elements.activeCandidate.textContent = `${candidate.taskType === "repaint" ? "Repaint" : "Candidate"} · ${shortId(candidate.candidateId)}`;
  elements.activeBadge.textContent = candidate.selected ? "현재 선택" : "미선택";
  elements.selectCandidate.textContent = candidate.selected ? "선택된 후보" : "이 후보 선택";
  elements.selectCandidate.disabled = candidate.selected || !candidate.artifactValid;
  elements.artifactState.textContent = candidate.artifactValid
    ? "SHA-256 · 길이 · PCM 확인"
    : candidate.artifactValidation;
  elements.artifactState.className = `artifact-state ${candidate.artifactValid ? "good" : "bad"}`;
  elements.duration.textContent = timeLabel(candidate.durationSeconds);
  elements.editRange.textContent = candidate.editRange
    ? `수정 ${candidate.editRange.startSeconds.toFixed(1)}–${candidate.editRange.endSeconds.toFixed(1)}초`
    : "전체 생성";
  renderFindings(candidate);
  renderReview(candidate);

  elements.audio.pause();
  if (candidate.audioUrl) {
    elements.audio.src = candidate.audioUrl;
    elements.audio.load();
    await new Promise<void>((resolve) => {
      elements.audio.addEventListener("loadedmetadata", () => resolve(), { once: true });
      elements.audio.addEventListener("error", () => resolve(), { once: true });
    });
    elements.audio.currentTime = Math.min(previousTime, Math.max(0, elements.audio.duration - 0.05));
    if (wasPlaying) await elements.audio.play().catch(() => undefined);
  } else {
    elements.audio.removeAttribute("src");
    elements.audio.load();
  }
  waveformSamples = Float32Array.from(candidate.waveform);
  drawWaveform();
}

function renderProject(next: ProjectState): void {
  const previousActive = activeCandidateId;
  state = next;
  elements.projectPath.textContent = next.projectPath ?? "프로젝트를 열어 주세요";
  const ready = next.status === "ready";
  elements.empty.classList.toggle("hidden", ready);
  elements.workspace.classList.toggle("hidden", !ready);
  if (!ready) {
    if (next.error) toast(next.error, true);
    return;
  }
  elements.candidateCount.textContent = String(next.candidates.length);
  elements.reviewedCount.textContent = String(
    next.candidates.filter((candidate) => candidate.humanReview.status !== "unreviewed").length,
  );
  const desired =
    next.candidates.find((candidate) => candidate.candidateId === previousActive)?.candidateId ??
    next.selectedCandidateId ??
    next.candidates[0]?.candidateId ??
    null;
  activeCandidateId = desired;
  renderCandidateList();
  if (desired) void activateCandidate(desired);
}

async function chooseProject(): Promise<void> {
  const next = await action("여는 중", () => window.musicApp.chooseProject());
  if (next) renderProject(next);
}

byId("open-project").addEventListener("click", () => void chooseProject());
byId("empty-open-project").addEventListener("click", () => void chooseProject());
byId("refresh").addEventListener("click", async () => {
  const next = await action("읽는 중", () => window.musicApp.refresh());
  if (next) renderProject(next);
});
byId("undo-selection").addEventListener("click", async () => {
  const next = await action("되돌리는 중", () => window.musicApp.undoSelection());
  if (next) renderProject(next);
});
elements.selectCandidate.addEventListener("click", async () => {
  const candidate = activeCandidate();
  if (!candidate) return;
  const next = await action("선택 저장 중", () => window.musicApp.select(candidate.candidateId));
  if (next) renderProject(next);
});
byId("save-review").addEventListener("click", async () => {
  const candidate = activeCandidate();
  if (!candidate) return;
  const rating = elements.reviewRating.value
    ? Number.parseInt(elements.reviewRating.value, 10)
    : undefined;
  const next = await action("청취 기록 저장 중", () =>
    window.musicApp.review({
      candidateId: candidate.candidateId,
      status: elements.reviewStatus.value as Review["status"],
      rating,
      note: elements.reviewNote.value.trim() || undefined,
    }),
  );
  if (next) renderProject(next);
});

elements.audio.addEventListener("timeupdate", () => {
  elements.currentTime.textContent = timeLabel(elements.audio.currentTime);
  drawWaveform();
});
elements.waveform.addEventListener("click", (event) => {
  if (!Number.isFinite(elements.audio.duration)) return;
  const rect = elements.waveform.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
  elements.audio.currentTime = ratio * elements.audio.duration;
});
window.addEventListener("resize", drawWaveform);

void action("복원 중", () => window.musicApp.getState()).then((next) => {
  if (next) renderProject(next);
});
