"""Loop copies: the seamless loop of a BGM version, cut from its listening copy.

Each loop is its own artifact (``loop-audio``) and job (``loop-copy``) that points at the
listening copy it was cut from; the version and its listening copy are never changed. Where
and how the cut is made is in :mod:`.loop_cut`. Like a listening copy, a loop is a render:
when the cut or the mastering changes, the new loop retires the old one, and a version keeps
one loop on disk. When no loop can be cut, the job records the reason and no file is made;
the same listening copy is not tried again until the cut itself changes.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from .jobs import ACTIVE_STATUSES, cleanup_export_outputs, recover_jobs
from .listening import ensure_listening_copy, listening_copies, played_versions, remove_retired
from .loop_cut import LOOP_VERSION, LoopNotFound, find_loop, render_loop
from .lyrics import is_instrumental_lyrics
from .qc import artifact_and_findings
from .storage import ProjectStore, new_id, utc_now

LOOP_KIND = "loop-audio"


def version_purpose(project: dict[str, Any], candidate: dict[str, Any]) -> str | None:
    request = ProjectStore.find_by_id(project, "requests", "requestId", candidate["requestId"])
    return request["parameters"].get("purpose")


def loop_copies(project: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The newest loop of each listening copy, by listening artifact id."""

    return {artifact["sourceArtifactId"]: artifact for artifact in project["artifacts"]
            if artifact.get("kind") == LOOP_KIND and artifact.get("sourceArtifactId") and not artifact.get("retired")}


def current_loop(store: ProjectStore, project: dict[str, Any], listening: dict[str, Any]) -> dict[str, Any] | None:
    """The loop cut from exactly this listening copy by the current cut, if its file verifies."""

    loop = loop_copies(project).get(listening["artifactId"])
    if (loop is not None and loop["loop"].get("version") == LOOP_VERSION
            and loop.get("sourceArtifactSha256") == listening["sha256"] and store.verify_artifact(loop)[0]):
        return loop
    return None


def loop_outcome(project: dict[str, Any], candidate_id: str) -> dict[str, Any] | None:
    """What the newest finished loop job of a version found, if any."""

    for job in reversed(project["jobs"]):
        if job["kind"] == "loop-copy" and job["parameters"].get("candidateId") == candidate_id and job["status"] not in ACTIVE_STATUSES:
            result = {"status": job["status"], **(job.get("outcome") or {})}
            if job["status"] != "succeeded":
                result["error"] = job.get("error")
            return result
    return None


def _found_nothing(project: dict[str, Any], candidate_id: str, listening_id: str) -> bool:
    return any(job["kind"] == "loop-copy" and job["status"] == "succeeded"
               and (job.get("outcome") or {}).get("status") == "none"
               and job["parameters"].get("candidateId") == candidate_id
               and job["parameters"].get("sourceArtifactId") == listening_id
               and job["parameters"].get("loopVersion") == LOOP_VERSION for job in project["jobs"])


def _reference_bpm(candidate: dict[str, Any], parameters: dict[str, Any]) -> float | None:
    bpm = parameters.get("bpm")
    if isinstance(bpm, (int, float)) and not isinstance(bpm, bool):
        return float(bpm)
    reference = (candidate.get("quality") or {}).get("playedTempo", {}).get("referenceBpm")
    return float(reference) if isinstance(reference, (int, float)) and not isinstance(reference, bool) else None


def _publish(destination: Path, write, on_staged) -> None:
    """Write through a durable temporary file, record it, then link it into place."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp.wav", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    staged = False
    try:
        write(temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        identity = temporary.stat()
        staged = True
        on_staged({"path": str(destination.absolute()), "temporaryPath": str(temporary.absolute()),
                   "device": identity.st_dev, "inode": identity.st_ino,
                   "expectedBytes": identity.st_size, "expectedSha256": digest})
        os.link(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if not staged:
            temporary.unlink(missing_ok=True)


def ensure_loop_copy(store: ProjectStore, candidate_id: str, *, parent_job_id: str | None = None) -> str | None:
    """Return the version's loop, cutting one if it has none that verifies; None when none can be cut."""

    project = store.load()
    candidate = store.find_by_id(project, "candidates", "candidateId", candidate_id)
    request = store.find_by_id(project, "requests", "requestId", candidate["requestId"])["parameters"]
    if not is_instrumental_lyrics(request.get("songPlan", {}).get("lyricsOriginal", request.get("lyrics", ""))):
        raise ValueError("only an instrumental version can be looped")
    version_artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
    listening = listening_copies(project).get(version_artifact["artifactId"])
    if listening is None or not store.verify_artifact(listening)[0]:
        raise ValueError("the version has no listening copy to cut a loop from")
    existing = current_loop(store, project, listening)
    if existing is not None:
        return existing["artifactId"]
    if _found_nothing(project, candidate_id, listening["artifactId"]):
        return None
    valid, reason = store.verify_artifact(version_artifact)
    if not valid:
        raise ValueError(reason)
    with store.transaction() as project:
        job = store.append_job(project, kind="loop-copy", parent_job_id=parent_job_id, parameters={
            "candidateId": candidate_id, "sourceArtifactId": listening["artifactId"],
            "analysisArtifactId": version_artifact["artifactId"], "loopVersion": LOOP_VERSION})
        job["ownedOutputs"] = []
        store.transition_job(job, "running", stage="finalizing: loop")
    job_id = job["jobId"]
    destination = store.root / "artifacts" / "loops" / f"{job_id}.wav"
    analysis = store.resolve_artifact(version_artifact)
    source = store.resolve_artifact(listening)
    try:
        try:
            plan = find_loop(analysis, source, reference_bpm=_reference_bpm(candidate, request),
                             time_signature=request.get("time_signature"))
        except LoopNotFound as missing:
            with store.transaction() as project:
                current = store.find_by_id(project, "jobs", "jobId", job_id)
                current["outcome"] = {"status": "none", "code": missing.code, "reason": missing.reason}
                store.transition_job(current, "succeeded", stage="no loop", progress=1)
            return None

        def staged(record: dict[str, Any]) -> None:
            with store.transaction() as project:
                store.find_by_id(project, "jobs", "jobId", job_id)["ownedOutputs"] = [record]

        rendered: dict[str, Any] = {}
        _publish(destination, lambda path: rendered.update(render_loop(source, path, plan, analysis=analysis)), staged)
        artifact, _ = artifact_and_findings(path=destination, project_relative_path=store.relative_path(destination),
                                            artifact_kind=LOOP_KIND, created_by_job_id=job_id,
                                            requested_duration_seconds=plan["durationSeconds"])
        warnings = rendered.pop("warnings")
        artifact.update(sourceArtifactId=listening["artifactId"], sourceArtifactSha256=listening["sha256"],
                        candidateId=candidate_id, loop={**plan, **rendered, "warnings": warnings})
        with store.transaction() as project:
            retired = [old for old in project["artifacts"] if old.get("kind") == LOOP_KIND and not old.get("retired")
                       and old.get("candidateId") == candidate_id]
            for old in retired:
                old["retired"] = {"supersededBy": artifact["artifactId"], "at": utc_now()}
            project["artifacts"].append(artifact)
            project["revisions"].append({"revisionId": new_id("revision"), "kind": "loop-copy",
                "previousRevisionId": project["revisions"][-1]["revisionId"] if project["revisions"] else None,
                "before": {"candidateId": candidate_id, "listeningArtifactId": listening["artifactId"]},
                "after": {"candidateId": candidate_id, "loopArtifactId": artifact["artifactId"]},
                "createdAt": utc_now()})
            current = store.find_by_id(project, "jobs", "jobId", job_id)
            current["resultRefs"] = [artifact["artifactId"]]
            current["outcome"] = {"status": "ready"}
            store.transition_job(current, "succeeded", stage="loop cut", progress=1)
        cleanup_export_outputs(current, include_published=False)
        for old in retired:
            remove_retired(store, old)
        return artifact["artifactId"]
    except (Exception, KeyboardInterrupt) as error:
        try:
            with store.transaction() as project:
                current = store.find_by_id(project, "jobs", "jobId", job_id)
                if current["status"] in ACTIVE_STATUSES:
                    current["outputCleanupErrors"] = cleanup_export_outputs(current)
                    state = "cancelled" if isinstance(error, KeyboardInterrupt) else "failed"
                    store.transition_job(current, state, stage=state, error=f"{type(error).__name__}: {error}")
        except (Exception, KeyboardInterrupt) as recording_error:
            error.add_note(f"Could not record loop failure: {recording_error}")
        raise


def loop_versions(project_root: Path | str, *, candidate_id: str | None = None) -> dict[str, Any]:
    """Cut the missing loops of a song's BGM versions, or of one named instrumental version."""

    store = ProjectStore(project_root)
    with store.generation_lock(lane=False):
        with store.transaction() as project:
            recover_jobs(store, project)
        if candidate_id:
            targets = [candidate_id]
        else:
            candidates = {item["candidateId"]: item for item in project["candidates"]}
            targets = [target for target in played_versions(project) if version_purpose(project, candidates[target]) == "bgm"]
        made, none, failed = {}, {}, {}
        for target in targets:
            try:
                ensure_listening_copy(store, target)
                artifact_id = ensure_loop_copy(store, target)
                if artifact_id is None:
                    none[target] = (loop_outcome(store.load(), target) or {}).get("reason")
                else:
                    made[target] = artifact_id
            except Exception as error:
                failed[target] = f"{type(error).__name__}: {error}"
    return {"loopArtifactIds": made, "noLoop": none, "failures": failed}
