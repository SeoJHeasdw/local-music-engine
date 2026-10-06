"""ACE launcher patches, checked with stand-ins for the ACE runtime (no torch/MLX/models)."""

import importlib.util
import inspect
import math
from pathlib import Path
import random
import sys
import types

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ace_api_server.py"
spec = importlib.util.spec_from_file_location("ace_api_server", MODULE_PATH)
launcher = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(launcher)


def install(monkeypatch, name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.mark.parametrize(("setting", "parameters", "expected"), [
    (None, 1_575_458_880, "float32"),  # turbo
    (None, 4_168_897_088, "bfloat16"),  # XL
    ("auto", 3_000_000_000, "float32"),
    ("float32", 4_168_897_088, "float32"),
    (" BFloat16 ", 1_575_458_880, "bfloat16"),
])
def test_mlx_dit_dtype(monkeypatch, setting, parameters, expected):
    if setting is None:
        monkeypatch.delenv("MUSIC_ENGINE_ACE_MLX_DIT_DTYPE", raising=False)
    else:
        monkeypatch.setenv("MUSIC_ENGINE_ACE_MLX_DIT_DTYPE", setting)
    assert launcher.mlx_dit_dtype(parameters) == expected


def test_invalid_mlx_dit_dtype_is_rejected(monkeypatch):
    monkeypatch.setenv("MUSIC_ENGINE_ACE_MLX_DIT_DTYPE", "float16")
    with pytest.raises(ValueError):
        launcher.mlx_dit_dtype(0)


class Vector:
    """One channel along the projected (time) axis; a length-1 vector broadcasts."""

    def __init__(self, values):
        self.values = list(values)

    def _zip(self, other, op):
        other = other.values if isinstance(other, Vector) else [other]
        size = max(len(self.values), len(other))
        return Vector(op(self.values[i % len(self.values)], other[i % len(other)]) for i in range(size))

    __add__ = lambda self, other: self._zip(other, lambda a, b: a + b)
    __radd__ = __add__
    __sub__ = lambda self, other: self._zip(other, lambda a, b: a - b)
    __mul__ = lambda self, other: self._zip(other, lambda a, b: a * b)
    __rmul__ = __mul__
    __truediv__ = lambda self, other: self._zip(other, lambda a, b: a / b)
    __rtruediv__ = lambda self, other: self._zip(other, lambda a, b: b / a)

    def sum(self, axis, keepdims):
        assert axis == 1 and keepdims
        return Vector([sum(self.values)])


def test_mlx_apg_uses_the_pytorch_momentum_buffer(monkeypatch):
    install(monkeypatch, "mlx")
    install(monkeypatch, "mlx.core", sqrt=lambda v: Vector(map(math.sqrt, v.values)),
            minimum=lambda a, b: a._zip(b, min), ones_like=lambda v: Vector([1.0] * len(v.values)))
    install(monkeypatch, "acestep")
    install(monkeypatch, "acestep.models")
    install(monkeypatch, "acestep.models.mlx")
    dit_generate = install(monkeypatch, "acestep.models.mlx.dit_generate", _mlx_apg_forward=None)
    launcher.match_pytorch_apg_momentum()

    def reference(cond, uncond, scale, buffer):
        # acestep/models/common/apg_guidance.py: MomentumBuffer(-0.75) + apg_forward(dims=[1]).
        buffer["running"] = [c - u - 0.75 * r for c, u, r in zip(cond, uncond, buffer["running"])]
        diff = buffer["running"]
        diff = [d * min(1.0, 2.5 / math.hypot(*diff)) for d in diff]
        unit = [c / math.hypot(*cond) for c in cond]
        parallel = sum(d * u for d, u in zip(diff, unit))
        return [c + (scale - 1) * (d - parallel * u) for c, d, u in zip(cond, diff, unit)]

    rng = random.Random(7)
    state, buffer = {}, {"running": [0.0] * 40}
    for _ in range(6):
        cond = [rng.gauss(0, 1) for _ in range(40)]
        uncond = [c + rng.gauss(0, 0.3) for c in cond]
        guided = dit_generate._mlx_apg_forward(Vector(cond), Vector(uncond), 7.0, state)
        assert guided.values == pytest.approx(reference(cond, uncond, 7.0, buffer), rel=1e-6, abs=1e-6)


def test_dcw_is_disabled_only_for_non_turbo_and_signature_survives(monkeypatch):
    calls = []

    class GenerateMusicMixin:
        turbo = True

        def is_turbo_model(self):
            return self.turbo

        def generate_music(self, captions, lyrics, dcw_enabled=True, shift=3.0):
            calls.append(dcw_enabled)

    install(monkeypatch, "loguru", logger=types.SimpleNamespace(info=lambda *args: None))
    for name in ("acestep", "acestep.core", "acestep.core.generation", "acestep.core.generation.handler"):
        install(monkeypatch, name)
    install(monkeypatch, "acestep.core.generation.handler.generate_music", GenerateMusicMixin=GenerateMusicMixin)
    launcher.disable_dcw_for_non_turbo()

    # ACE filters kwargs through this signature; a bare *args/**kwargs wrapper would drop them all.
    assert {"dcw_enabled", "shift"} <= set(inspect.signature(GenerateMusicMixin.generate_music).parameters)
    handler = GenerateMusicMixin()
    handler.generate_music(captions="c", lyrics="l", dcw_enabled=True)
    handler.turbo = False
    handler.generate_music(captions="c", lyrics="l", dcw_enabled=True)
    handler.generate_music(captions="c", lyrics="l", dcw_enabled=False)
    assert calls == [True, False, False]
