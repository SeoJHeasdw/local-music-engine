import { build } from "esbuild";
import { copyFile, mkdir, rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const appRoot = path.dirname(fileURLToPath(import.meta.url));
const output = path.join(appRoot, "dist");
await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });

await Promise.all([
  build({
    entryPoints: [path.join(appRoot, "main.ts")],
    outfile: path.join(output, "main.mjs"),
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node22",
    external: ["electron"],
    sourcemap: true,
  }),
  build({
    entryPoints: [path.join(appRoot, "preload.ts")],
    outfile: path.join(output, "preload.cjs"),
    bundle: true,
    platform: "node",
    format: "cjs",
    target: "node22",
    external: ["electron"],
    sourcemap: true,
  }),
  build({
    entryPoints: [path.join(appRoot, "renderer", "app.ts")],
    outfile: path.join(output, "renderer.js"),
    bundle: true,
    platform: "browser",
    format: "iife",
    target: "chrome142",
    sourcemap: true,
  }),
]);

await Promise.all(
  ["index.html", "tokens.css", "app.css"].map((file) =>
    copyFile(path.join(appRoot, "renderer", file), path.join(output, file)),
  ),
);
