import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from local_music_engine import lane
from local_music_engine.lane import ExclusiveLane, LaneBusy, shared_lane
from local_music_engine.service.jobs import JobStore, StoreBusy
from local_music_engine.service.runner import Runner
from local_music_engine.service.server import Api, _handler
from local_music_engine.storage import ProjectStore

LOCAL = {"host": "127.0.0.1:47833"}


class Service:
    def __init__(self, root: Path, *, resident=lambda: None):
        self.store = JobStore(root / "output" / "jobs")
        self.runner = Runner(self.store)
        self.api = Api(self.store, self.runner, port=47833, resident=resident)

    def call(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        return self.api.handle(method, path, {**LOCAL, **(headers or {})}, data)

    def submit(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        status, job = self.call("POST", "/api/jobs", {"recipe": "selftest", "params": params or {}})
        assert status == 201, job
        return job

    def wait(self, job_id: str, states=frozenset({"done", "failed", "cancelled"}), timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            job = self.store.load(job_id)
            if job["state"] in states or time.monotonic() > deadline:
                return job
            time.sleep(0.02)


@pytest.fixture
def service(tmp_path: Path):
    running = Service(tmp_path)
    running.runner.start()
    yield running
    running.runner.shutdown()


def test_health_and_recipes_follow_the_contract(service: Service) -> None:
    status, health = service.call("GET", "/api/health")
    assert status == 200
    assert health == {"ok": True, "engine": "music", "version": health["version"], "contract": 0, "busy": False,
                      "resident": False, "currentJob": None, "outputDir": str(service.store.root.parent)}
    status, body = service.call("GET", "/api/recipes")
    assert status == 200
    (selftest,) = body["recipes"]
    assert selftest["name"] == "selftest" and selftest["needsReview"] is False
    assert selftest["paramsSchema"]["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(selftest["paramsSchema"]["properties"]) == {"steps", "stepSeconds", "failAt"}


@pytest.mark.parametrize("body", [
    {"recipe": "no-such-recipe"},
    {"recipe": "selftest", "params": []},
    {"recipe": "selftest", "params": {"steps": 0}},
    {"recipe": "selftest", "params": {"steps": True}},
    {"recipe": "selftest", "params": {"steps": 2, "failAt": 3}},
    {"recipe": "selftest", "params": {"stepSeconds": "nan"}},
    b"[1]",
    b"{not json",
])
def test_input_errors_are_400_and_leave_no_record(service: Service, body: Any) -> None:
    status, answer = service.call("POST", "/api/jobs", body)
    assert status == 400 and answer["detail"]
    assert service.store.list() == []


def test_selftest_runs_through_stages_and_leaves_an_audio_asset_and_a_finding(service: Service) -> None:
    job = service.submit({"steps": "2", "stepSeconds": 0.3, "unknown": 1})
    assert job["params"] == {"steps": 2, "stepSeconds": 0.3}
    assert Path(job["dir"]) == service.store.root / job["id"]
    running = service.wait(job["id"], {"running"})
    assert running["state"] == "running"
    _, health = service.call("GET", "/api/health")
    assert health["currentJob"] == job["id"] and health["busy"] is True and "busyBy" not in health
    assert health["resident"] is False

    done = service.wait(job["id"])
    assert done["state"] == "done" and done["error"] is None and done["errorCode"] is None
    assert [stage["state"] for stage in done["stages"]] == ["done", "done"]
    assert all(stage["progress"] == 1.0 and stage["seconds"] >= 0.25 and stage["peakMemoryBytes"] is None
               for stage in done["stages"])
    (asset,) = done["assets"]
    assert asset["kind"] == "audio" and asset["review"] == "pending"
    assert (Path(done["dir"]) / asset["file"]).read_bytes()[:4] == b"RIFF"
    (finding,) = done["findings"]
    assert finding["review"] == "pending" and finding["evidence"] == [asset["file"]] and finding["assetId"] == asset["id"]
    _, health = service.call("GET", "/api/health")
    assert health["currentJob"] is None and health["busy"] is False


def test_cancel_stops_a_running_job_and_closes_a_queued_one(service: Service) -> None:
    first = service.submit({"steps": 2, "stepSeconds": 5})
    second = service.submit({"steps": 1, "stepSeconds": 0})
    service.wait(first["id"], {"running"})
    status, queued = service.call("POST", f"/api/jobs/{second['id']}/cancel")
    assert status == 200 and queued["state"] == "cancelled" and queued["finishedAt"]
    status, answered = service.call("POST", f"/api/jobs/{first['id']}/cancel")
    assert status == 200 and answered["state"] in {"cancelling", "cancelled"}
    final = service.wait(first["id"])
    assert final["state"] == "cancelled" and final["errorCode"] is None
    assert final["stages"][-1]["state"] == "cancelled"
    assert service.store.load(second["id"])["startedAt"] is None
    assert service.call("POST", "/api/jobs/20260101-000000-abcd/cancel")[0] == 404


def test_fail_at_ends_failed_with_engine_error(service: Service) -> None:
    job = service.submit({"steps": 2, "stepSeconds": 0.1, "failAt": 2})
    final = service.wait(job["id"])
    assert final["state"] == "failed" and final["errorCode"] == "engine_error" and "failAt=2" in final["error"]
    assert [stage["state"] for stage in final["stages"]] == ["done", "failed"]
    assert final["logTail"]


def test_reviews_are_the_persons_and_bound_to_the_file(service: Service) -> None:
    done = service.wait(service.submit({"stepSeconds": 0})["id"])
    job_id, asset, finding = done["id"], done["assets"][0], done["findings"][0]
    path = f"/api/jobs/{job_id}/assets/{asset['id']}/review"

    status, record = service.call("POST", path, {"status": "approved"})
    reviewed = record["assets"][0]
    assert status == 200 and reviewed["review"] == "approved" and len(reviewed["reviewedSha256"]) == 64
    assert record["findings"][0]["review"] == "pending"  # 에셋 판단은 확인 항목을 바꾸지 않는다

    # 승인한 뒤 파일이 바뀌면 그 승인은 내보내지 않는다.
    (Path(done["dir"]) / asset["file"]).write_bytes(b"RIFF changed")
    shown = service.store.load(job_id)["assets"][0]
    assert shown["review"] == "pending" and "reviewedSha256" not in shown
    status, record = service.call("POST", path, {"status": "pending"})
    assert status == 200 and "reviewedSha256" not in record["assets"][0]

    assert service.call("POST", path, {"status": "looks-fine"})[0] == 400
    assert service.call("POST", f"/api/jobs/{job_id}/assets/a99/review", {"status": "approved"})[0] == 404
    finding_path = f"/api/jobs/{job_id}/findings/{finding['id']}/review"
    status, record = service.call("POST", finding_path, {"status": "cleared"})
    assert status == 200 and record["findings"][0]["review"] == "cleared"
    assert record["assets"][0]["review"] == "pending"
    assert service.call("POST", finding_path, {"status": "approved"})[0] == 400
    assert service.call("POST", f"/api/jobs/{job_id}/findings/f99/review", {"status": "cleared"})[0] == 404


def test_lists_newest_first_and_answers_unknown_jobs(service: Service) -> None:
    ids = [service.submit({"stepSeconds": 0})["id"] for _ in range(3)]
    for job_id in ids:
        service.wait(job_id)
    status, body = service.call("GET", "/api/jobs?limit=2")
    assert status == 200 and len(body["jobs"]) == 2
    assert [job["id"] for job in service.store.list()] == sorted(ids, reverse=True)
    assert service.call("GET", "/api/jobs/no-such-job")[0] == 404
    assert service.call("GET", "/api/jobs/../../etc")[0] == 404
    assert service.call("GET", "/api/nothing")[0] == 404
    assert service.call("DELETE", "/api/jobs")[0] == 405


@pytest.mark.parametrize("headers", [
    {"origin": "http://example.invalid"},
    {"origin": "http://127.0.0.1:9999"},
    {"origin": "null"},
    {"host": "attacker.example:47833"},
    {"sec-fetch-site": "cross-site"},
])
def test_only_local_same_origin_requests_are_served(service: Service, headers: dict[str, str]) -> None:
    status, body = service.call("GET", "/api/health", headers=headers)
    assert status == 403 and body["detail"]
    assert service.call("GET", "/api/health", headers={"origin": "http://127.0.0.1:47833"})[0] == 200


def test_a_cli_generation_holds_service_jobs_queued_and_shows_as_busy(service: Service) -> None:
    with shared_lane():
        _, health = service.call("GET", "/api/health")
        assert health["busy"] is True and health["busyBy"] == "lane:generation.lock" and health["currentJob"] is None
        assert health["resident"] is False
        job = service.submit({"stepSeconds": 0})
        time.sleep(0.5)
        assert service.store.load(job["id"])["state"] == "queued"
    assert service.wait(job["id"])["state"] == "done"
    assert service.call("GET", "/api/health")[1]["busy"] is False


def test_cli_generation_is_refused_while_a_service_job_runs(tmp_path: Path) -> None:
    project = ProjectStore.initialize(tmp_path / "song", title="t", lyrics="la", style_prompt="pop",
                                      target_duration_seconds=10)
    held = ExclusiveLane()
    assert held.try_acquire()
    try:
        with pytest.raises(LaneBusy, match="service is running a job"):
            with shared_lane(wait_seconds=0.1):
                pass
        with pytest.raises(LaneBusy):
            with project.generation_lock():
                pass
        with project.generation_lock(lane=False):  # 정본만 고치는 복구는 막지 않는다
            pass
    finally:
        held.release()
    # CLI 실행끼리는 지금처럼 함께 돈다.
    with shared_lane(), project.generation_lock():
        with shared_lane(wait_seconds=0):
            assert ExclusiveLane().try_acquire() is False


def test_cli_waits_out_a_health_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lane, "SHARED_WAIT_SECONDS", 2.0)
    probe = ExclusiveLane()
    assert probe.try_acquire()
    threading.Timer(0.2, probe.release).start()
    with shared_lane():
        pass


def test_a_resident_model_server_is_busy_and_resident_until_work_starts(tmp_path: Path) -> None:
    service = Service(tmp_path, resident=lambda: "ace-step:18001")
    _, health = service.call("GET", "/api/health")
    assert health["busy"] is True and health["busyBy"] == "ace-step:18001" and health["currentJob"] is None
    assert health["resident"] is True
    # 일이 시작되면 상주가 아니다. CLI 생성이 차선을 쥐었거나 서비스 작업이 돈다.
    with shared_lane():
        _, health = service.call("GET", "/api/health")
        assert health["busyBy"] == "lane:generation.lock" and health["resident"] is False
    service.runner.start()
    try:
        job = service.submit({"stepSeconds": 0.5})
        service.wait(job["id"], {"running"})
        _, health = service.call("GET", "/api/health")
        assert health["currentJob"] == job["id"] and health["resident"] is False and "busyBy" not in health
        service.wait(job["id"])
        _, health = service.call("GET", "/api/health")
        assert health["currentJob"] is None and health["busy"] is True and health["resident"] is True
    finally:
        service.runner.shutdown()


def test_stopping_interrupts_running_and_queued_jobs_but_keeps_cancels(tmp_path: Path) -> None:
    service = Service(tmp_path)
    service.runner.start()
    running = service.submit({"steps": 1, "stepSeconds": 10})
    queued = service.submit({"stepSeconds": 0})
    service.wait(running["id"], {"running"})
    service.runner.shutdown()
    assert service.store.load(running["id"])["state"] == "failed"
    assert service.store.load(running["id"])["errorCode"] == "interrupted"
    assert service.store.load(running["id"])["stages"][0]["state"] == "failed"
    assert service.store.load(queued["id"])["errorCode"] == "interrupted"
    assert service.call("POST", "/api/jobs", {"recipe": "selftest"})[0] == 409

    cancelled = Service(tmp_path)
    cancelled.runner.start()
    job = cancelled.submit({"steps": 1, "stepSeconds": 10})
    cancelled.wait(job["id"], {"running"})
    cancelled.store.update(job["id"], lambda record: record.update(state="cancelling"))
    cancelled.runner.shutdown()
    assert cancelled.store.load(job["id"])["state"] == "cancelled"
    assert cancelled.store.load(job["id"])["stages"][0]["state"] == "cancelled"


def test_restart_closes_what_a_dead_service_left_and_drops_finished_selftests(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    left = {state: store.create("song", {}, "t")["id"] for state in ("queued", "running", "cancelling")}
    for state, job_id in left.items():
        store.update(job_id, lambda record, state=state: record.update(
            state=state, stages=[{"name": "step-1", "state": "running"}]))
    finished = store.create("selftest", {}, "t")["id"]
    store.update(finished, lambda record: record.update(state="done"))
    other = store.create("song", {}, "t")["id"]
    store.update(other, lambda record: record.update(state="done"))

    runner = Runner(store)
    runner.start()
    runner.shutdown()
    assert store.load(left["queued"])["state"] == "failed" and store.load(left["queued"])["errorCode"] == "interrupted"
    assert store.load(left["running"])["errorCode"] == "interrupted"
    assert store.load(left["running"])["stages"][0]["state"] == "failed"
    assert store.load(left["cancelling"])["state"] == "cancelled"
    assert store.load(left["cancelling"])["errorCode"] is None
    assert not (store.root / finished).exists() and store.load(other)["state"] == "done"


def test_one_service_per_output_folder(tmp_path: Path) -> None:
    first = JobStore(tmp_path / "jobs")
    first.claim()
    try:
        with pytest.raises(StoreBusy):
            JobStore(tmp_path / "jobs").claim()
    finally:
        first.release()
    JobStore(tmp_path / "jobs").claim()


def test_http_transport_answers_json_errors(service: Service) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(service.api))
    port = server.server_address[1]
    service.api.port = port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health") as response:
            assert response.status == 200 and json.load(response)["engine"] == "music"
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/jobs", method="POST",
                                         data=json.dumps({"recipe": "selftest", "params": {"stepSeconds": 0}}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            assert response.status == 201 and json.load(response)["state"] in {"queued", "running"}
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/jobs/no-such-job")
        assert missing.value.code == 404 and json.load(missing.value) == {"detail": "작업을 찾을 수 없습니다."}
    finally:
        server.shutdown()
        server.server_close()
