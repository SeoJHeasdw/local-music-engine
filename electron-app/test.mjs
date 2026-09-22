import { build } from "esbuild";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

const root = path.dirname(fileURLToPath(import.meta.url));
const directory = await mkdtemp(path.join(tmpdir(), "music-app-tests-"));
try {
  const entries = (await readdir(path.join(root, "tests"))).filter((name) => name.endsWith(".test.ts"));
  await build({
    entryPoints: entries.map((name) => path.join(root, "tests", name)),
    outdir: directory, outExtension: { ".js": ".mjs" },
    bundle: true, platform: "node", format: "esm", target: "node22", external: ["electron"],
  });
  const child = spawn(process.execPath, ["--test", ...entries.map((name) => path.join(directory, name.replace(/\.ts$/, ".mjs")))], { stdio: "inherit" });
  process.exitCode = await new Promise((resolve) => child.once("exit", (code) => resolve(code ?? 1)));
} finally {
  await rm(directory, { recursive: true, force: true });
}
