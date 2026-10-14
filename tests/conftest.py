import pytest

from local_music_engine.lane import LANE_ENV


@pytest.fixture(autouse=True)
def private_work_lane(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never meet a running service's lane on this machine, and never block it."""

    monkeypatch.setenv(LANE_ENV, str(tmp_path_factory.mktemp("lane") / "generation.lock"))
