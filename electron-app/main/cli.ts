import { spawn, type ChildProcess } from "node:child_process";
import { cliPath, engineRoot } from "./paths.ts";

// Main never re-implements engine logic: every mutation goes through the same CLI a
// person or coding agent would run, and the CLI prints exactly one JSON document.

export function spawnCli(args: string[]): ChildProcess {
  return spawn(cliPath, args, {
    cwd: engineRoot,
    env: { ...process.env, PYTHONUTF8: "1", PYTHONUNBUFFERED: "1" },
    stdio: ["ignore", "pipe", "pipe"],
    shell: false,
  });
}

export function collectCli(child: ChildProcess): Promise<{ code: number | null; signal: NodeJS.Signals | null; stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    let stdout = "";
    let stderr = "";
    child.stdout?.setEncoding("utf8");
    child.stderr?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr?.on("data", (chunk: string) => {
      stderr += chunk;
    });
    child.once("error", reject);
    child.once("close", (code, signal) => resolve({ code, signal, stdout, stderr }));
  });
}

export async function runCli<T = unknown>(args: string[]): Promise<T> {
  const { code, stdout, stderr } = await collectCli(spawnCli(args));
  if (code !== 0) throw new Error(humanizeError(stderr.trim() || `music-engine exited with ${code}`));
  try {
    return JSON.parse(stdout) as T;
  } catch {
    throw new Error("엔진이 읽을 수 없는 결과를 보냈어요.");
  }
}

// Translate the engine's English errors into what the person can do about them. Unknown
// errors pass through so nothing is hidden.
export function humanizeError(raw: string): string {
  const message = raw.replace(/^error:\s*/i, "").replace(/^[A-Za-z]+Error:\s*/, "").trim();
  const table: Array<[RegExp, string]> = [
    [/project generation is already running/i, "이 곡을 다른 창이나 터미널에서 만드는 중이에요. 끝난 뒤 다시 시도하세요."],
    [/export output already exists/i, "같은 이름의 파일이 있어요. 이전 내보내기를 보존하도록 새 이름을 골라 주세요."],
    [/export output must be outside/i, "원본과 기록을 보존하도록 곡 폴더 밖에 내보내 주세요."],
    [/ACE API unavailable|Connection refused|unhealthy ACE/i, "음악 엔진이 꺼져 있어요. 왼쪽 아래에서 엔진을 켠 뒤 다시 시도하세요."],
    [/LLM not initialized|LLM init failed/i, "음악 엔진의 작사 모델이 준비되지 않았어요. 엔진을 다시 켜 보세요."],
    [/ACE task timed out/i, "엔진이 제한 시간 안에 곡을 끝내지 못했어요."],
    [/project not found/i, "곡 폴더에서 project.json을 찾지 못했어요."],
    [/project already exists/i, "같은 폴더에 이미 곡이 있어요."],
    [/missing artifact file|artifact hash changed|artifact size changed/i, "음원 파일이 없거나 만들어진 뒤 바뀌었어요. 이 버전은 쓸 수 없어요."],
    [/no candidate selection can be undone/i, "되돌릴 최종본 지정이 없어요."],
    [/no candidate is selected/i, "먼저 최종본을 지정하세요."],
    [/repaint end exceeds audio duration/i, "고칠 구간이 곡 길이를 넘어요."],
    [/describe the song/i, "어떤 곡인지 한 줄이라도 적어 주세요."],
  ];
  for (const [pattern, text] of table) if (pattern.test(message)) return text;
  return message;
}
