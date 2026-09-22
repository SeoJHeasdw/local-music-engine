import path from "node:path";
import { fileURLToPath } from "node:url";

// esbuild bundles main into dist/main.mjs, so paths resolve from there.
export const distRoot = path.dirname(fileURLToPath(import.meta.url));
export const engineRoot = path.resolve(distRoot, "../..");
export const cliPath = path.join(engineRoot, ".venv", "bin", "music-engine");
export const aceStartScript = path.join(engineRoot, "scripts", "start_ace_api.sh");
export const aceApiBinary = path.join(engineRoot, ".runtime", "ace-step-1.5", ".venv", "bin", "acestep-api");
export const defaultProjectsDir = path.join(engineRoot, "projects");
