import json
import os
import time
from contextlib import contextmanager
from types import SimpleNamespace

from web.service.timelapse import TimelapseService, _IN_PROGRESS_SUBDIR


class FakeConfigManager:
    def __init__(self, root, enabled=True):
        self.config_root = root
        self._cfg = SimpleNamespace(
            timelapse={
                "enabled": enabled,
                "interval": 5,
                "max_videos": 2,
                "save_persistent": True,
                "light": None,
            }
        )

    @contextmanager
    def open(self):
        yield self._cfg


def test_timelapse_meta_and_video_file_helpers(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)

    meta_dir = tmp_path / "capture"
    meta_dir.mkdir()
    svc._write_meta(meta_dir, "cube.gcode", 3)

    assert svc._read_meta(meta_dir) == {"filename": "cube.gcode", "frame_count": 3}

    video_a = tmp_path / "a.mp4"
    video_b = tmp_path / "b.mp4"
    video_a.write_bytes(b"a")
    time.sleep(0.01)
    video_b.write_bytes(b"bb")

    videos = svc.list_videos()
    assert [video["filename"] for video in videos] == ["b.mp4", "a.mp4"]
    assert svc.get_video_path("a.mp4") == str(video_a)
    assert svc.get_video_path("../a.mp4") is None
    assert svc.delete_video("a.mp4") is True
    assert not video_a.exists()


def test_timelapse_prunes_old_videos(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)

    for name in ("old.mp4", "mid.mp4", "new.mp4"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        time.sleep(0.01)

    svc._prune_old_videos()

    remaining = sorted(p.name for p in tmp_path.glob("*.mp4"))
    assert remaining == ["mid.mp4", "new.mp4"]


def test_timelapse_scan_recovers_or_resumes_in_progress(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    base = tmp_path / _IN_PROGRESS_SUBDIR
    base.mkdir()

    young = base / "young_capture"
    young.mkdir()
    (young / "frame_00000.jpg").write_bytes(b"x")
    (young / "frame_00001.jpg").write_bytes(b"y")
    with open(young / ".meta", "w") as fh:
        json.dump({"filename": "young.gcode", "frame_count": 2}, fh)

    old = base / "old_capture"
    old.mkdir()
    (old / "frame_00000.jpg").write_bytes(b"x")
    (old / "frame_00001.jpg").write_bytes(b"y")
    with open(old / ".meta", "w") as fh:
        json.dump({"filename": "old.gcode", "frame_count": 2}, fh)
    stale_time = time.time() - (25 * 3600)
    os.utime(old, (stale_time, stale_time))

    scheduled = []
    assembled = []
    pruned = []

    monkeypatch.setattr(TimelapseService, "_schedule_finalize", lambda self, d, f, c, suffix="": scheduled.append((d, f, c, suffix)))
    monkeypatch.setattr(TimelapseService, "_assemble_video_from", lambda self, d, f, c, suffix="": assembled.append((d, f, c, suffix)))
    monkeypatch.setattr(TimelapseService, "_prune_old_videos", lambda self: pruned.append(True))

    svc = TimelapseService(cfg, captures_dir=tmp_path)

    assert svc._resume_filename == "young.gcode"
    assert svc._resume_frame_count == 2
    assert scheduled and scheduled[0][1] == "young.gcode"
    assert assembled and assembled[0][1] == "old.gcode" and assembled[0][3] == "_recovered"
    assert pruned == [True]


def test_timelapse_finish_and_fail_paths(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    svc._current_dir = str(tmp_path / "current")
    svc._current_filename = "cube.gcode"
    svc._frame_count = 3
    os.makedirs(svc._current_dir, exist_ok=True)

    finalize_calls = []
    disable_calls = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr("web.service.timelapse.threading.Thread", FakeThread)
    monkeypatch.setattr(TimelapseService, "_stop_capture_thread", lambda self: None)
    monkeypatch.setattr(TimelapseService, "_cancel_pending_resume", lambda self: None)
    monkeypatch.setattr(TimelapseService, "_disable_video_for_timelapse", lambda self: disable_calls.append(True))
    monkeypatch.setattr(TimelapseService, "_assemble_video_from", lambda self, d, f, c, suffix="": finalize_calls.append((d, f, c, suffix)))
    monkeypatch.setattr(TimelapseService, "_prune_old_videos", lambda self: finalize_calls.append(("prune",)))
    monkeypatch.setattr(TimelapseService, "_cleanup_dir", lambda self, d: finalize_calls.append(("cleanup", d)))

    svc.finish_capture(final=True)

    assert ("prune",) in finalize_calls
    assert any(call[:4] == (str(tmp_path / "current"), "cube.gcode", 3, "") for call in finalize_calls if len(call) == 4)
    assert disable_calls == [True]

    svc._current_dir = str(tmp_path / "failed")
    svc._current_filename = "failed.gcode"
    svc._frame_count = 2
    os.makedirs(svc._current_dir, exist_ok=True)
    finalize_calls.clear()
    disable_calls.clear()

    svc.fail_capture()

    assert any(call[:4] == (str(tmp_path / "failed"), "failed.gcode", 2, "_partial") for call in finalize_calls if len(call) == 4)
    assert disable_calls == [True]
