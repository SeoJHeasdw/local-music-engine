import path from "node:path";
import { fileURLToPath } from "node:url";

// esbuild bundles main into dist/main.mjs, so paths resolve from there.
export const distRoot = path.dirname(fileURLToPath(import.meta.url));
export const engineRoot = path.resolve(distRoot, "../..");
export const cliPath = path.join(engineRoot, ".venv", "bin", "music-engine");
export const aceStartScript = path.join(engineRoot, "scripts", "start_ace_api.sh");
export const acePython = path.join(engineRoot, ".runtime", "ace-step-1.5", ".venv", "bin", "python");
export const aceServer = path.join(engineRoot, "scripts", "ace_api_server.py");
export const defaultProjectsDir = path.join(engineRoot, "projects");
