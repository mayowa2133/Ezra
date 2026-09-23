import subprocess
from pathlib import Path

import pytest

from clipper.clipping import framing, render
from clipper.clipping.framing import Shot, plan_shots


def samples(xs, width=0.1):
    """One sample every 0.5s; None = no face found."""
    return [(i / framing.SAMPLE_FPS, [] if x is None else [(x, width)]) for i, x in enumerate(xs)]


def test_steady_speaker_is_one_shot():
    shots = plan_shots(samples([0.30, 0.31, 0.29, 0.30, 0.32, 0.30]), 3.0)
    assert len(shots) == 1 and shots[0].start == 0 and shots[0].end == 3.0
    assert shots[0].x == pytest.approx(0.30, abs=0.01)


def test_camera_cut_starts_a_new_shot():
    shots = plan_shots(samples([0.3] * 8 + [0.75] * 8), 8.0)
    assert [(s.start, s.end) for s in shots] == [(0.0, 4.0), (4.0, 8.0)]
    assert shots[0].x == pytest.approx(0.3) and shots[1].x == pytest.approx(0.75)


def test_single_misdetection_does_not_move_the_crop():
    shots = plan_shots(samples([0.3, 0.3, 0.3, 0.9, 0.3, 0.3, 0.3]), 3.5)
    assert len(shots) == 1 and shots[0].x == pytest.approx(0.3)


def test_noisy_alternation_does_not_thrash():
    shots = plan_shots(samples([0.3, 0.8, 0.3, 0.8, 0.3, 0.8, 0.3, 0.8]), 4.0)
    assert len(shots) == 1


def test_largest_face_wins_and_gaps_are_filled():
    s = [(0.0, [(0.2, 0.05), (0.7, 0.15)]), (0.5, []), (1.0, [(0.7, 0.15)]), (1.5, [])]
    shots = plan_shots(s, 2.0)
    assert len(shots) == 1 and shots[0].x == pytest.approx(0.7)


def test_no_faces_falls_back_to_centre():
    assert plan_shots(samples([None, None, None]), 1.5) == [Shot(0.0, 1.5, 0.5)]


def test_crop_expression_switches_at_shot_boundaries():
    expr = framing.crop_x_expr([Shot(0, 4, 0.3), Shot(4, 8, 0.75)])
    assert expr == "if(lt(t,4.000),max(0,min(iw-ow,0.3000*iw-ow/2)),max(0,min(iw-ow,0.7500*iw-ow/2)))"


def _mean_rgb(video: Path, t: float, tmp: Path) -> tuple[int, int, int]:
    from PIL import Image, ImageStat
    png = tmp / f"f{t}.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(video),
                    "-frames:v", "1", "-vf", "scale=54:96", str(png)], check=True)
    return tuple(int(v) for v in ImageStat.Stat(Image.open(png).convert("RGB")).mean)


def test_tracked_crop_really_moves_in_ffmpeg(tmp_path, monkeypatch):
    src = tmp_path / "halves.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=red:size=960x1080:rate=25",
                    "-f", "lavfi", "-i", "color=c=blue:size=960x1080:rate=25",
                    "-filter_complex", "[0][1]hstack", "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
                    str(src)], check=True)
    monkeypatch.setattr(framing, "auto_shots", lambda *a: [Shot(0, 2, 0.2), Shot(2, 4, 0.8)])
    out = render.render(str(src), tmp_path / "out.mp4", 0, 4, None, framing="crop", captions=False)
    r1, _, b1 = _mean_rgb(out, 1.0, tmp_path)
    r3, _, b3 = _mean_rgb(out, 3.0, tmp_path)
    assert r1 > 200 and b1 < 60, "first shot should frame the left (red) speaker"
    assert b3 > 200 and r3 < 60, "second shot should frame the right (blue) speaker"


def test_detector_runs_on_real_video_and_finds_no_faces_in_a_test_pattern(video):
    got = framing.detect_faces(str(video), 0, 3)
    assert got is not None and len(got) == 6 and all(faces == [] for _, faces in got)


def test_real_face_is_tracked_across_a_camera_cut(tmp_path):
    """A real (public-domain NASA) portrait, left of frame then right of frame."""
    skimage_data = pytest.importorskip("skimage.data")
    from PIL import Image

    face = Image.fromarray(skimage_data.astronaut()).resize((420, 420))
    for name, x in (("left", 180), ("right", 1320)):
        canvas = Image.new("RGB", (1920, 1080), (40, 50, 60))
        canvas.paste(face, (x, 330))
        canvas.save(tmp_path / f"{name}.png")
    src = tmp_path / "cut.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-loop", "1", "-t", "4", "-i", str(tmp_path / "left.png"),
                    "-loop", "1", "-t", "4", "-i", str(tmp_path / "right.png"),
                    "-filter_complex", "[0][1]concat=n=2:v=1:a=0,format=yuv420p", "-r", "25",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    shots = framing.auto_shots(str(src), 0, 8)
    assert [(s.start, s.end) for s in shots] == [(0.0, 4.0), (4.0, 8.0)]
    assert shots[0].x == pytest.approx(0.203, abs=0.03) and shots[1].x == pytest.approx(0.797, abs=0.03)
