import path from "node:path";
import { fileURLToPath } from "node:url";

// esbuild bundles main into dist/main.mjs, so paths resolve from there.
export const distRoot = path.dirname(fileURLToPath(import.meta.url));
export const engineRoot = path.resolve(distRoot, "../..");
export const cliPath = path.join(engineRoot, ".venv", "bin", "music-engine");
export const music3StartScript = path.join(engineRoot, "scripts", "start_music3_api.sh");
export const music3Python = path.join(engineRoot, ".runtime", "minimax-music3", ".venv", "bin", "python");
export const music3Server = path.join(engineRoot, "scripts", "music3_api_server.py");
export const defaultProjectsDir = path.join(engineRoot, "projects");
