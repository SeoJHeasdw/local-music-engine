import assert from "node:assert/strict";
import { test } from "node:test";
import { humanizeError } from "../main/cli.ts";

test("a generation refused for a running service job says to wait for it", () => {
  assert.equal(
    humanizeError("error: LaneBusy: the music engine service is running a job; try again when it finishes"),
    "음악 엔진 작업 서비스가 맡은 일을 하는 중이에요. 끝난 뒤 다시 시도하세요.",
  );
});

test("a run that could not bring ACE up says why, not to switch the engine on", () => {
  assert.match(humanizeError("error: AceApiError: ACE engine did not start (exit 1): ACE runtime is missing. Run ./scripts/bootstrap_ace.sh first."),
    /설치되지 않았어요/);
  assert.match(humanizeError("error: AceApiError: ACE engine did not start (exit 1): Traceback"), /엔진을 켜지 못했어요/);
  assert.match(humanizeError("error: AceUnavailable: ACE API unavailable: [Errno 61] Connection refused"), /연결이 끊겼어요/);
  for (const raw of ["ACE engine did not start", "ACE API unavailable", "ACE server has DiT x loaded, not the requested y"]) {
    assert.doesNotMatch(humanizeError(raw), /엔진을 켠 뒤|켜세요/);
  }
});
