"""What the service accepts: each recipe validates its params and runs as one job.

``prepare`` runs at submission, so input errors are a 400 and leave no record.
``run`` gets a :class:`~.runner.JobContext` and records stages, assets and findings.
"""

from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .runner import JobContext

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


class InvalidInput(ValueError):
    """The params cannot run. At submission this is a 400; during a run, ``invalid_input``."""


class EngineFailure(RuntimeError):
    """The engine could not finish the job (``engine_error``)."""


@dataclass(frozen=True)
class Recipe:
    name: str
    label: str
    params_schema: dict[str, Any]
    needs_review: bool
    prepare: Callable[[dict[str, Any]], tuple[dict[str, Any], str]]
    run: Callable[["JobContext"], None]
    # 결과물이 아닌 점검 작업. 서비스가 시작할 때 끝난 기록과 폴더를 지운다.
    diagnostic: bool = field(default=False)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "paramsSchema": self.params_schema,
                "needsReview": self.needs_review}


def int_param(params: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise InvalidInput(f"{key}는 정수여야 합니다.") from None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInput(f"{key}는 정수여야 합니다.")
    if not low <= value <= high:
        raise InvalidInput(f"{key}는 {low}~{high} 사이여야 합니다.")
    return value


def number_param(params: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise InvalidInput(f"{key}는 숫자여야 합니다.") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidInput(f"{key}는 숫자여야 합니다.")
    if not low <= value <= high:
        raise InvalidInput(f"{key}는 {low:g}~{high:g} 사이여야 합니다.")
    return float(value)


# ── selftest ────────────────────────────────────────────────────────────────
# 계약 검사가 모델 없이 대기열·진행률·취소·실패·기록을 확인하는 레시피다. ACE 서버를 부르지 않는다.

SELFTEST_MAX_STEPS = 10
SELFTEST_MAX_STEP_SECONDS = 30.0
SELFTEST_SAMPLE_RATE = 48_000


def _prepare_selftest(params: dict[str, Any]) -> tuple[dict[str, Any], str]:
    steps = int_param(params, "steps", 2, 1, SELFTEST_MAX_STEPS)
    normalized: dict[str, Any] = {
        "steps": steps,
        "stepSeconds": number_param(params, "stepSeconds", 0.5, 0.0, SELFTEST_MAX_STEP_SECONDS),
    }
    if params.get("failAt") is not None:
        normalized["failAt"] = int_param(params, "failAt", 1, 1, steps)
    return normalized, "엔진 자체 점검"


def _write_check_tone(path, seconds: float = 0.5) -> None:
    """A short quiet 440 Hz tone, so the sample asset is real audio like any song."""

    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(SELFTEST_SAMPLE_RATE * seconds)
    samples = (int(0.1 * 32767 * math.sin(2 * math.pi * 440 * index / SELFTEST_SAMPLE_RATE)) for index in range(frames))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SELFTEST_SAMPLE_RATE)
        handle.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


def _run_selftest(ctx: "JobContext") -> None:
    params = ctx.params
    for step in range(1, params["steps"] + 1):
        with ctx.stage(f"step-{step}", f"점검 {step}단계") as stage:
            if params.get("failAt") == step:
                raise EngineFailure(f"selftest가 failAt={step}에 따라 {step}단계에서 실패했습니다.")
            stage.wait(params["stepSeconds"])
    tone = ctx.dir / "selftest" / "check.wav"
    _write_check_tone(tone)
    asset = ctx.add_asset(kind="audio", role="final", file=tone, meta={"seconds": 0.5, "frequencyHz": 440})
    # 계약 검사가 findings의 모양을 볼 수 있게 남기는 표본이다. 실제 검사 결과가 아니다.
    ctx.add_finding(severity="warn", code="selftest.sample", params={"step": 1}, where="점검 1단계 · 00:00",
                    asset_id=asset["id"], evidence=[tone])


SELFTEST = Recipe(
    name="selftest",
    label="엔진 자체 점검",
    params_schema={
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "description": "모델과 ACE 서버 없이 몇 초 안에 끝나는 계약 점검. 짧은 점검음 하나와 표본 확인 항목 하나를 남긴다.",
        "properties": {
            "steps": {"type": "integer", "minimum": 1, "maximum": SELFTEST_MAX_STEPS, "default": 2},
            "stepSeconds": {"type": "number", "minimum": 0, "maximum": SELFTEST_MAX_STEP_SECONDS, "default": 0.5},
            "failAt": {"type": "integer", "minimum": 1, "maximum": SELFTEST_MAX_STEPS,
                       "description": "이 단계(1부터, steps 이하)에서 engine_error로 실패한다"},
        },
    },
    needs_review=False,
    prepare=_prepare_selftest,
    run=_run_selftest,
    diagnostic=True,
)

RECIPES: dict[str, Recipe] = {recipe.name: recipe for recipe in (SELFTEST,)}
