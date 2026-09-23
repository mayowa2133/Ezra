"""The ffmpeg side of rendering: one filtergraph per clip.

    source ─(EDL trim/concat)─▶ edited cut @30fps
      ├─ track stream  (crop following the face, per shot)
      ├─ blur stream   (fit over blurred fill)          ─ overlaid per scene
      └─ split stream  (two stacked crops)              ─ overlaid per scene
      → punch-in zoom → B-roll cutaways → logo / watermark → hook card
      → caption layer (raw RGBA piped from Python) → CTA card
    audio: EDL-trimmed, loudness-normalized (EBU R128, -14 LUFS)
    then optional intro/outro concat, thumbnail pick, SRT/ASS sidecars.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..analysis.faces import FaceDetector, get_detector, sample_faces
from ..transcription.base import Word
from . import captions as cap
from . import edl, layout
from .spec import RenderSpec

FPS = 30
Progress = Callable[[float, str], None]


class RenderError(RuntimeError):
    pass


@dataclass
class RenderResult:
    video: Path
    duration: float
    width: int
    height: int
    layout: str
    edit_summary: dict[str, Any]
    srt: Path | None = None
    ass: Path | None = None
    thumbnail: Path | None = None
    words: list[Word] = field(default_factory=list)
    seconds: float = 0.0


def _run(cmd: list[str], what: str, timeout: int = 3600) -> None:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RenderError(f"{what} failed: {out.stderr.strip()[-1200:]}")


def probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    d = json.loads(out.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), {})
    return {"duration": float(d["format"]["duration"]), "width": v.get("width"), "height": v.get("height"),
            "has_audio": any(s["codec_type"] == "audio" for s in d["streams"])}


def cut_media(src: Path, pieces: list[edl.Piece], out: Path, has_audio: bool) -> Path:
    """Trim + concat the EDL pieces into one constant-frame-rate intermediate.
    Input seeking starts near the first piece; trims are relative to that seek."""
    seek = max(0.0, pieces[0].src_start - 2.0)
    parts, labels = [], []
    for i, p in enumerate(pieces):
        a, b = p.src_start - seek, p.src_end - seek
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,fps={FPS}[v{i}]")
        labels.append(f"[v{i}]")
        if has_audio:
            parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
            labels.append(f"[a{i}]")
    parts.append("".join(labels) + f"concat=n={len(pieces)}:v=1:a={1 if has_audio else 0}"
                 + ("[v][a]" if has_audio else "[v]"))
    dest = out.with_suffix(".mkv")
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{seek:.3f}", "-t", f"{pieces[-1].src_end - seek + 0.5:.3f}",
           "-i", str(src), "-filter_complex", ";".join(parts), "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "[a]", "-c:a", "pcm_s16le"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-pix_fmt", "yuv420p", str(dest)]
    _run(cmd, "edit cut")
    return dest


def _png(img: Image.Image, path: Path) -> Path:
    img.save(path)
    return path


def compose(src: Path, start: float, end: float, words: list[Word], scene_cuts_src: list[float],
            spec: RenderSpec, out: Path, storage_path: Callable[[str], Path],
            detector: FaceDetector | None = None, progress: Progress | None = None) -> RenderResult:
    say = progress or (lambda f, m: None)
    t0 = time.time()
    W, H = spec.size
    safe = spec.resolved_safe_zone()
    info = probe(src)
    has_audio = info["has_audio"]
    with tempfile.TemporaryDirectory(prefix="ezra-render-") as tmp:
        tdir = Path(tmp)
        # 1. edit decision list -------------------------------------------------------------
        pieces, summary = edl.build(words, start, end, spec.remove_silence, spec.silence_threshold,
                                    spec.remove_fillers)
        duration = edl.output_duration(pieces)
        edited = len(pieces) > 1 or summary["removed_seconds"] > 0.01
        say(0.05, "cutting")
        if edited:
            media = cut_media(src, pieces, tdir / "cut", has_audio)
            in_args = ["-i", str(media)]
            out_words = edl.remap_words(words, pieces)
            cuts = [t for c in scene_cuts_src if start < c < end and (t := edl.map_time(pieces, c)) is not None]
        else:
            media = src
            in_args = ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src)]
            out_words = [Word(w.w, round(w.s - start, 3), round(w.e - start, 3), w.p, w.spk)
                         for w in words if w.e > start and w.s < end and not edl.is_filler(w.w)]
            cuts = [c - start for c in scene_cuts_src if start < c < end]

        # 2. reframing plan ---------------------------------------------------------------
        say(0.15, "finding faces")
        need_faces = spec.layout in ("auto", "track", "split") and spec.crop_x is None and info["width"]
        samples = sample_faces(media, 0 if edited else start, duration if edited else end, fps=2.0,
                               detector=detector or get_detector()) if need_faces else []
        plans = layout.plan(samples, duration, cuts, spec.layout, spec.aspect, 2.0, spec.crop_x) \
            if info["width"] else [layout.ScenePlan(0, duration, "blur")]
        modes = {p.mode for p in plans}

        # 3. filter graph -----------------------------------------------------------------
        inputs: list[str] = list(in_args)
        graph: list[str] = []
        n_in = 1
        streams = [m for m in ("track", "blur", "split") if m in modes or (m == "track" and modes & {"static", "center"})]
        if not streams:
            streams = ["blur"]
        graph.append(f"[0:v]fps={FPS},setsar=1,split={len(streams)}" + "".join(f"[in_{m}]" for m in streams))
        if "track" in streams:
            pcs = []
            for p in plans:
                pcs += [(s.start, s.end, s.x) for s in p.shots] if p.shots else [(p.start, p.end, 0.5)]
            graph.append(f"[in_track]scale={W}:{H}:force_original_aspect_ratio=increase,"
                         f"crop={W}:{H}:'{layout.x_expr(pcs)}':'(ih-oh)/2'[s_track]")
        if "blur" in streams:
            graph.append(f"[in_blur]split[bb][bf];[bb]scale={W}:{H}:force_original_aspect_ratio=increase,"
                         f"crop={W}:{H},gblur=sigma=28[bg];[bf]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
                         f"[bg][fg]overlay=(W-w)/2:(H-h)/2[s_blur]")
        if "split" in streams:
            sp = [p for p in plans if p.mode == "split" and p.split_x]
            h2 = H // 2
            # zoom each half so a face fills ~26% of the width (wide two-shots have small faces)
            src_w, src_h = info["width"], info["height"]
            base_w = src_w * h2 / src_h if src_w / src_h >= W / h2 else W
            fw = sorted(p.face_w for p in sp if p.face_w)[len(sp) // 2] if any(p.face_w for p in sp) else None
            zoom = max(1.0, min(2.4, 0.26 * W / (fw * base_w))) if fw else 1.0
            sw, sh = int(base_w * zoom) // 2 * 2, int(base_w * zoom * src_h / src_w) // 2 * 2

            def half(idx: int) -> str:
                xs = layout.x_expr([(p.start, p.end, p.split_x[idx]) for p in sp])  # type: ignore[index]
                ys = layout.x_expr([(p.start, p.end, (p.split_y or (0.45, 0.45))[idx]) for p in sp], axis="y")
                return f"scale={max(sw, W)}:{max(sh, h2)},crop={W}:{h2}:'{xs}':'{ys}'"

            graph.append(f"[in_split]split[sa][sb];[sa]{half(0)}[st];[sb]{half(1)}[sbo];[st][sbo]vstack[s_split]")
        base = f"s_{streams[0]}"
        for m in streams[1:]:
            ranges = [(p.start, p.end) for p in plans if p.mode == m]
            graph.append(f"[{base}][s_{m}]overlay=enable='{layout.enable_expr(ranges)}'[c_{m}]")
            base = f"c_{m}"

        cur = base
        k = 0

        def nxt() -> str:
            nonlocal k
            k += 1
            return f"L{k}"

        # punch-in: jump zoom on the hook or on emphasis words
        zoom_ranges: list[tuple[float, float]] = []
        if spec.punch_in == "hook":
            zoom_ranges = [(0.0, min(1.4, duration))]
        elif spec.punch_in == "emphasis":
            last = -10.0
            for w in out_words:
                if cap.is_keyword(w.w, {x.lower() for x in spec.highlight_keywords}) and w.s - last > 4.0:
                    zoom_ranges.append((max(0.0, w.s - 0.1), min(duration, w.s + 1.1)))
                    last = w.s
        if zoom_ranges:
            z = f"if({layout.enable_expr(zoom_ranges)},1.12,1)"
            o = nxt()
            graph.append(f"[{cur}]scale=w='trunc({W}*{z}/2)*2':h='trunc({H}*{z}/2)*2':eval=frame,"
                         f"crop={W}:{H}:(iw-ow)/2:(ih-oh)/2,setsar=1[{o}]")
            cur = o

        # B-roll cutaways (authorized assets only; audio of the clip continues)
        for b in spec.broll:
            if b.at >= duration:
                continue
            dur = min(b.duration, duration - b.at)
            inputs += ["-t", f"{dur:.3f}", "-i", str(storage_path(b.asset_key))]
            o, bl = nxt(), nxt()
            graph.append(f"[{n_in}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},"
                         f"setpts=PTS-STARTPTS+{b.at:.3f}/TB[{bl}];[{cur}][{bl}]overlay=enable='between(t,{b.at:.3f},"
                         f"{b.at + dur:.3f})':eof_action=pass[{o}]")
            n_in += 1
            cur = o

        # logo & watermark
        margin_x = int(W * max(0.03, safe["left"] * 0.6))
        top_y = int(H * safe["top"])
        if spec.logo_key:
            inputs += ["-i", str(storage_path(spec.logo_key))]
            o, lg = nxt(), nxt()
            lw = int(W * 0.16)
            x = f"{margin_x}" if "left" in spec.logo_position else f"W-w-{int(W * max(0.03, safe['right'] * 0.6))}"
            y = f"{top_y}" if "top" in spec.logo_position else f"H-h-{int(H * safe['bottom'])}"
            graph.append(f"[{n_in}:v]scale={lw}:-1,format=rgba[{lg}];[{cur}][{lg}]overlay={x}:{y}[{o}]")
            n_in += 1
            cur = o
        if spec.watermark:
            wm = _png(cap.text_badge(spec.watermark, W, int(W * 0.028)), tdir / "wm.png")
            inputs += ["-i", str(wm)]
            o = nxt()
            graph.append(f"[{cur}][{n_in}:v]overlay={margin_x}:H-h-{int(H * safe['bottom'] * 0.55)}[{o}]")
            n_in += 1
            cur = o

        # hook card for the first seconds
        hook = cap.hook_card(spec.hook_text or "", W, spec.caption_font) if spec.hook_overlay and spec.hook_text else None
        if hook is not None:
            inputs += ["-i", str(_png(hook, tdir / "hook.png"))]
            o = nxt()
            graph.append(f"[{cur}][{n_in}:v]overlay=(W-w)/2:{top_y + int(H * 0.02)}:"
                         f"enable='lt(t,{min(3.5, duration):.3f})'[{o}]")
            n_in += 1
            cur = o

        # caption layer (streamed)
        renderer = None
        if spec.captions and out_words:
            # split layouts put captions on the seam, where they cover nobody's face
            position = spec.caption_position or (0.5 if "split" in modes else None)
            renderer = cap.CaptionRenderer(out_words, spec.caption_theme, W, H, safe, font=spec.caption_font,
                                           size=spec.caption_size, position=position,
                                           colors=spec.colors, keywords=spec.highlight_keywords)
            inputs += ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{renderer.band_h}", "-r", str(FPS),
                       "-i", "pipe:0"]
            o = nxt()
            graph.append(f"[{cur}][{n_in}:v]overlay=0:{renderer.band_y}:eof_action=pass[{o}]")
            n_in += 1
            cur = o

        # CTA end card
        if spec.cta_text:
            inputs += ["-i", str(_png(cap.cta_card(spec.cta_text, W, (spec.colors or {}).get("active")),
                                      tdir / "cta.png"))]
            o = nxt()
            graph.append(f"[{cur}][{n_in}:v]overlay=(W-w)/2:{int(H * 0.36)}:"
                         f"enable='gte(t,{max(0.0, duration - 3.0):.3f})'[{o}]")
            n_in += 1
            cur = o
        graph.append(f"[{cur}]format=yuv420p[vout]")
        maps = ["-map", "[vout]"]
        if has_audio:
            af = "loudnorm=I=-14:TP=-1.5:LRA=11," if spec.normalize_audio else ""
            graph.append(f"[0:a]{af}aresample=48000[aout]")
            maps += ["-map", "[aout]"]
        body = tdir / "body.mp4"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", ";".join(graph),
               *maps, "-t", f"{duration:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(body)]
        say(0.3, "rendering")
        errlog = tdir / "ffmpeg.log"
        with open(errlog, "w") as errf:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if renderer else subprocess.DEVNULL, stderr=errf)
            writer_error: list[BaseException] = []
            if renderer is not None:
                assert proc.stdin is not None

                def feed() -> None:
                    try:
                        renderer.stream(duration, FPS, proc.stdin.write)  # type: ignore[union-attr]
                    except (BrokenPipeError, ValueError) as e:  # ffmpeg exited early; its log explains why
                        writer_error.append(e)
                    finally:
                        try:
                            proc.stdin.close()  # type: ignore[union-attr]
                        except BrokenPipeError:
                            pass

                th = threading.Thread(target=feed, daemon=True)
                th.start()
                th.join()
            code = proc.wait()
        if code != 0:
            raise RenderError(f"ffmpeg render failed ({code}): {errlog.read_text()[-1500:]}")
        final = body

        # 4. intro / outro ----------------------------------------------------------------
        if spec.intro_key or spec.outro_key:
            say(0.85, "adding intro/outro")
            final = _bookend(body, storage_path(spec.intro_key) if spec.intro_key else None,
                             storage_path(spec.outro_key) if spec.outro_key else None, W, H, tdir / "final.mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        final.replace(out)
        say(0.9, "thumbnail and captions")
        thumb = pick_thumbnail(out, out.with_suffix(".jpg")) if spec.thumbnail else None
        srt = out.with_suffix(".srt")
        srt.write_text(cap.to_srt(out_words))
        ass = out.with_suffix(".ass")
        ass.write_text(cap.to_ass(out_words, spec.caption_theme, W, H))
    real = probe(out)
    return RenderResult(out, real["duration"], W, H, layout.summary(plans),
                        {**summary, "scenes": [{"start": p.start, "end": p.end, "mode": p.mode, "faces": p.faces}
                                               for p in plans],
                         "punch_in": zoom_ranges, "broll": [b.model_dump() for b in spec.broll]},
                        srt, ass, thumb, out_words, time.time() - t0)


def _bookend(body: Path, intro: Path | None, outro: Path | None, W: int, H: int, out: Path) -> Path:
    files = [p for p in (intro, body, outro) if p is not None]
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for f in files:
        cmd += ["-i", str(f)]
    parts, labels = [], []
    for i, f in enumerate(files):
        parts.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
                     f"setsar=1,fps={FPS},format=yuv420p[v{i}]")
        if probe(f)["has_audio"]:
            parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:
            d = probe(f)["duration"]
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={d:.3f}[a{i}]")
        labels += [f"[v{i}]", f"[a{i}]"]
    parts.append("".join(labels) + f"concat=n={len(files)}:v=1:a=1[v][a]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset",
            "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    _run(cmd, "intro/outro")
    return out


def pick_thumbnail(video: Path, out: Path) -> Path:
    """Sharpest frame with a well-placed face, skipping the first half second."""
    import cv2

    d = probe(video)["duration"]
    cap_ = cv2.VideoCapture(str(video))
    best, best_score = None, -1.0
    try:
        det = get_detector("haar")
    except Exception:
        det = None
    for t in np.linspace(min(0.6, d / 2), max(0.6, d - 0.5), num=12):
        cap_.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000)
        ok, frame = cap_.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (360, int(360 * gray.shape[0] / gray.shape[1])))
        sharp = float(cv2.Laplacian(small, cv2.CV_64F).var())
        face_bonus = 0.0
        if det is not None:
            faces = det.detect(cv2.resize(gray, (960, int(960 * gray.shape[0] / gray.shape[1]))))
            if faces:
                f = max(faces, key=lambda f: f.w)
                face_bonus = 400 * (1 - abs(f.cx - 0.5)) * min(1.0, f.w * 4)
        score = sharp + face_bonus
        if score > best_score:
            best, best_score = frame, score
    cap_.release()
    if best is None:
        raise RenderError("could not read any frame for the thumbnail")
    cv2.imwrite(str(out), best, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return out
