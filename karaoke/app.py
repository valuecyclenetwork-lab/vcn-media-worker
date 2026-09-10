"""
VCN Karaoke Worker  (vcn-karaoke-worker)  —  Stage 3 of the ₦1,000 Music Package.

A SEPARATE Railway service. It never touches the proven Full HD Music Video
worker (render_music_video.py) and shares nothing with it except the HMAC
request convention (X-VCN-Timestamp / X-VCN-Signature over "<ts>.<raw body>").

Pipeline (sequential, one job at a time — Demucs + WhisperX are RAM heavy):
  1. download the member's private source MP3 (signed URL) + cover art
  2. Demucs `htdemucs`  → instrumental (drums+bass+other) + vocals stem
  3. lyric timing:
       • stored VCN lyrics present  → WhisperX FORCED ALIGNMENT of that text
         against the *vocals* stem (authoritative words, word-level timings)
       • no stored lyrics           → Whisper transcription of the vocals stem,
         then alignment (flagged lyrics_source="transcribed")
  4. ASS subtitles: line-level highlight by default, word-level karaoke (\\k)
     only when alignment confidence supports it
  5. FFmpeg: cover background + subtitles burnt in + instrumental audio →
     1920×1080 / 30 fps H.264 + AAC stereo MP4
  6. upload MP4 (+ instrumental MP3 + .ass) to the signed VCN upload URLs,
     then POST a signed callback to VCN.

Endpoints (all signed unless noted):
  GET  /health                       (public: liveness + model readiness)
  POST /v1/render-karaoke
  POST /v1/job-status
  POST /v1/job-diagnostics

Env:
  VCN_KARAOKE_WORKER_SECRET  (falls back to VCN_MEDIA_WORKER_SECRET)
  KARAOKE_DEVICE             cpu | cuda            (default cpu)
  KARAOKE_WHISPER_MODEL      small | medium | ...  (default small)
  KARAOKE_MAX_SKEW           seconds               (default 900)
  PORT
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

SECRET = os.environ.get("VCN_KARAOKE_WORKER_SECRET") or os.environ.get("VCN_MEDIA_WORKER_SECRET", "")
MAX_SKEW = int(os.environ.get("KARAOKE_MAX_SKEW", "900"))
DEVICE = os.environ.get("KARAOKE_DEVICE", "cpu")
WHISPER_MODEL = os.environ.get("KARAOKE_WHISPER_MODEL", "small")
DEMUCS_MODEL = "htdemucs"
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]
FONT_NAME = "DejaVu Sans"

app = FastAPI(title="VCN Karaoke Worker")

JOBS: Dict[str, Dict[str, Any]] = {}
DIAGNOSTICS: Dict[str, Dict[str, Any]] = {}
QUEUE: "asyncio.Queue[Tuple[str, Dict[str, Any]]]" = asyncio.Queue()
MODELS: Dict[str, Any] = {}


# ----------------------------------------------------------------- security
def _sign(timestamp: str, raw: str) -> str:
    return hmac.new(SECRET.encode(), f"{timestamp}.{raw}".encode(), hashlib.sha256).hexdigest()


async def _verified_body(request: Request, timestamp: Optional[str], signature: Optional[str]) -> Dict[str, Any]:
    if not SECRET:
        raise HTTPException(status_code=500, detail="Worker is not configured")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Unsigned request")
    try:
        skew = abs(time.time() - float(timestamp))
    except ValueError:
        raise HTTPException(status_code=401, detail="Bad timestamp")
    if skew > MAX_SKEW:
        raise HTTPException(status_code=401, detail="Stale request")
    raw = (await request.body()).decode()
    if not hmac.compare_digest(_sign(timestamp, raw), signature.strip().lower()):
        raise HTTPException(status_code=401, detail="Bad signature")
    return json.loads(raw or "{}")


def _redact(text: str) -> str:
    out = str(text or "")
    out = re.sub(r"https?://\S+", "[redacted-url]", out)
    if SECRET:
        out = out.replace(SECRET, "[redacted-secret]")
    out = re.sub(r"(?i)(token|signature|apikey|api_key|secret)=\S+", r"\1=[redacted]", out)
    return out


# ------------------------------------------------------------------ helpers
def _font() -> Optional[str]:
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    return None


def _run(cmd: List[str], timeout: int, stage: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = _redact(proc.stderr or "")[-1200:]
        raise RuntimeError(f"{stage} failed (rc={proc.returncode}): {tail}")
    return proc


def _probe_duration(path: str) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except Exception:  # noqa: BLE001
        return 0.0


async def _download(client: httpx.AsyncClient, url: str, dest: str) -> None:
    async with client.stream("GET", url, follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            async for chunk in r.aiter_bytes(1 << 20):
                fh.write(chunk)


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


# ------------------------------------------------------------------- models
def _load_models() -> None:
    """Loads Whisper/WhisperX once. Demucs runs via its CLI (own process, frees RAM)."""
    if "whisperx" in MODELS:
        return
    import whisperx  # type: ignore

    compute = "float16" if DEVICE == "cuda" else "int8"
    MODELS["whisperx"] = whisperx
    MODELS["asr"] = whisperx.load_model(WHISPER_MODEL, DEVICE, compute_type=compute)
    MODELS["align_cache"] = {}


def _align_model(language: str):
    whisperx = MODELS["whisperx"]
    cache = MODELS["align_cache"]
    if language not in cache:
        cache[language] = whisperx.load_align_model(language_code=language, device=DEVICE)
    return cache[language]


# ---------------------------------------------------------------- pipeline
def _separate(work: str, src: str, diag: Dict[str, Any]) -> Tuple[str, str]:
    """Demucs htdemucs → (instrumental.wav, vocals.wav)."""
    t0 = time.time()
    out = os.path.join(work, "demucs")
    _run(
        ["python", "-m", "demucs", "-n", DEMUCS_MODEL, "--two-stems", "vocals",
         "-d", DEVICE, "-o", out, "--filename", "{stem}.{ext}", src],
        timeout=2400, stage="vocal separation",
    )
    base = os.path.join(out, DEMUCS_MODEL)
    vocals = os.path.join(base, "vocals.wav")
    inst = os.path.join(base, "no_vocals.wav")
    if not (os.path.exists(vocals) and os.path.exists(inst)):
        raise RuntimeError("vocal separation produced no stems")
    diag["separation_seconds"] = round(time.time() - t0, 1)
    return inst, vocals


def _normalise_lyrics(text: str) -> List[str]:
    lines: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # drop section markers such as [Chorus], (Verse 2), **Bridge**
        if re.fullmatch(r"[\[\(\*]+\s*[A-Za-z0-9 \-:']+\s*[\]\)\*]+", line):
            continue
        lines.append(line)
    return lines


def _time_lyrics(vocals: str, lyrics: str, language_hint: Optional[str], diag: Dict[str, Any]) -> Dict[str, Any]:
    """
    Returns {"lines":[{"start","end","text","words":[{"word","start","end","score"}]}],
             "mode": "word"|"line", "confidence": float, "coverage": float,
             "language": str, "source": "stored"|"transcribed"}
    """
    t0 = time.time()
    _load_models()
    whisperx = MODELS["whisperx"]
    audio = whisperx.load_audio(vocals)
    lines = _normalise_lyrics(lyrics)
    source = "stored" if lines else "transcribed"

    # 1) language + rough segments from ASR (also gives text when no lyrics exist)
    asr = MODELS["asr"].transcribe(audio, batch_size=8, language=(language_hint or None))
    language = asr.get("language") or language_hint or "en"
    segments = asr.get("segments") or []
    diag["asr_seconds"] = round(time.time() - t0, 1)
    diag["language"] = language

    if not lines:
        lines = [s["text"].strip() for s in segments if s.get("text", "").strip()]
        if not lines:
            raise RuntimeError("no lyrics available and transcription found no words")

    # 2) FORCED ALIGNMENT of the authoritative text: replace ASR text with the
    #    stored lyrics distributed over the sung region, then align word timings.
    total = float(len(audio)) / 16000.0
    sung_start = float(segments[0]["start"]) if segments else 0.0
    sung_end = float(segments[-1]["end"]) if segments else total
    sung_end = max(sung_end, sung_start + 1.0)
    span = sung_end - sung_start
    weights = [max(len(l.split()), 1) for l in lines]
    wsum = float(sum(weights))
    forced: List[Dict[str, Any]] = []
    cursor = sung_start
    for l, w in zip(lines, weights):
        dur = span * (w / wsum)
        forced.append({"start": cursor, "end": cursor + dur, "text": l})
        cursor += dur

    try:
        model_a, meta = _align_model(language)
        aligned = whisperx.align(forced, model_a, meta, audio, DEVICE, return_char_alignments=False)
        seg_out = aligned.get("segments") or []
    except Exception as exc:  # noqa: BLE001  (unsupported language etc.)
        diag["align_error"] = _redact(str(exc))[:300]
        seg_out = []

    out_lines: List[Dict[str, Any]] = []
    scores: List[float] = []
    timed_words = 0
    total_words = 0
    for i, line in enumerate(lines):
        seg = seg_out[i] if i < len(seg_out) else None
        words = []
        if seg:
            for w in seg.get("words") or []:
                total_words += 1
                if "start" in w and "end" in w:
                    timed_words += 1
                    scores.append(float(w.get("score", 0.0)))
                    words.append({"word": w["word"], "start": float(w["start"]), "end": float(w["end"]),
                                  "score": float(w.get("score", 0.0))})
        start = float(seg["start"]) if seg and seg.get("start") is not None else forced[i]["start"]
        end = float(seg["end"]) if seg and seg.get("end") is not None else forced[i]["end"]
        if words:
            start, end = min(start, words[0]["start"]), max(end, words[-1]["end"])
        out_lines.append({"start": start, "end": max(end, start + 0.8), "text": line, "words": words})

    confidence = (sum(scores) / len(scores)) if scores else 0.0
    coverage = (timed_words / total_words) if total_words else 0.0
    mode = "word" if (confidence >= 0.6 and coverage >= 0.85 and source == "stored") else "line"
    diag["alignment_seconds"] = round(time.time() - t0 - diag.get("asr_seconds", 0), 1)
    return {"lines": out_lines, "mode": mode, "confidence": round(confidence, 3),
            "coverage": round(coverage, 3), "language": language, "source": source}


def _write_ass(path: str, timing: Dict[str, Any], title: str, username: str, width: int, height: int) -> None:
    mode = timing["mode"]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Lyric,{FONT_NAME},72,&H00FFFFFF,&H0000D7FF,&H00101820,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,80,80,140,1
Style: Next,{FONT_NAME},48,&H90FFFFFF,&H90FFFFFF,&H00101820,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,80,80,60,1
Style: Meta,{FONT_NAME},34,&HB0FFFFFF,&HB0FFFFFF,&H00101820,&H80000000,0,0,0,0,100,100,0,0,1,2,0,7,60,60,50,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = timing["lines"]
    events: List[str] = []
    end_all = lines[-1]["end"] + 4 if lines else 10
    events.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(end_all)},Meta,,0,0,0,,{_ass_escape(title)}  ·  {_ass_escape(username)}  ·  VCN Karaoke")
    for i, ln in enumerate(lines):
        start = ln["start"] - 0.3 if i == 0 else max(ln["start"] - 0.3, lines[i - 1]["end"])
        end = ln["end"] + 0.4
        if i + 1 < len(lines):
            end = min(end, lines[i + 1]["start"] + 0.2)
        if mode == "word" and ln["words"]:
            parts = []
            prev = ln["words"][0]["start"]
            lead = max(int((prev - start) * 100), 0)
            if lead:
                parts.append(f"{{\\k{lead}}}")
            for w in ln["words"]:
                gap = max(int((w["start"] - prev) * 100), 0)
                if gap:
                    parts.append(f"{{\\k{gap}}}")
                dur = max(int((w["end"] - w["start"]) * 100), 8)
                parts.append(f"{{\\kf{dur}}}{_ass_escape(w['word'])} ")
                prev = w["end"]
            text = "".join(parts).rstrip()
        else:
            text = f"{{\\fad(150,150)}}{_ass_escape(ln['text'])}"
        events.append(f"Dialogue: 1,{_ass_time(start)},{_ass_time(end)},Lyric,,0,0,0,,{text}")
        if i + 1 < len(lines):
            nxt = lines[i + 1]["text"]
            events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Next,,0,0,0,,{_ass_escape(nxt)}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(events) + "\n")


def _render(work: str, cover: Optional[str], inst: str, ass: str, out_mp4: str,
            width: int, height: int, fps: int, duration: float, diag: Dict[str, Any]) -> None:
    t0 = time.time()
    ass_f = ass.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    fontsdir = os.path.dirname(_font() or "") or "/usr/share/fonts"
    if cover and os.path.exists(cover):
        vf = (
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=30:2,eq=brightness=-0.25:saturation=1.1,"
            f"format=yuv420p[bg];"
            f"[bg]ass='{ass_f}':fontsdir='{fontsdir}',fps={fps}[v]"
        )
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-i", inst, "-loop", "1", "-framerate", str(fps), "-i", cover,
               "-filter_complex", vf, "-map", "[v]", "-map", "0:a",
               "-t", f"{duration:.3f}"]
    else:
        vf = f"color=c=0x101820:s={width}x{height}:r={fps},ass='{ass_f}':fontsdir='{fontsdir}'[v]"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-i", inst, "-filter_complex", vf, "-map", "[v]", "-map", "0:a",
               "-t", f"{duration:.3f}"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "44100",
            "-movflags", "+faststart", "-shortest", out_mp4]
    _run(cmd, timeout=1800, stage="karaoke video render")
    diag["render_seconds"] = round(time.time() - t0, 1)


async def _process(kid: str, body: Dict[str, Any]) -> None:
    work = tempfile.mkdtemp(prefix="vcnk_")
    diag: Dict[str, Any] = {"started_at": time.time()}
    DIAGNOSTICS[kid] = diag
    stage = "download"
    try:
        JOBS[kid] = {"status": "PROCESSING", "stage": stage}
        width = int(body.get("width", 1920)); height = int(body.get("height", 1080)); fps = int(body.get("fps", 30))
        src = os.path.join(work, "source.mp3")
        cover = os.path.join(work, "cover.img")
        async with httpx.AsyncClient() as client:
            await _download(client, body["audio_url"], src)
            if body.get("cover_url"):
                try:
                    await _download(client, body["cover_url"], cover)
                except Exception:  # noqa: BLE001
                    cover = ""
        duration = _probe_duration(src)
        if duration <= 1:
            raise RuntimeError("source audio unreadable")
        diag["source_duration"] = round(duration, 3)

        stage = "vocal separation"; JOBS[kid]["stage"] = stage
        inst_wav, vocals_wav = await asyncio.to_thread(_separate, work, src, diag)

        stage = "lyric alignment"; JOBS[kid]["stage"] = stage
        timing = await asyncio.to_thread(_time_lyrics, vocals_wav, body.get("lyrics", ""), body.get("language_hint"), diag)

        stage = "subtitles"; JOBS[kid]["stage"] = stage
        ass = os.path.join(work, "lyrics.ass")
        _write_ass(ass, timing, body.get("title", ""), body.get("creator_username", ""), width, height)

        stage = "instrumental encode"; JOBS[kid]["stage"] = stage
        inst_mp3 = os.path.join(work, "instrumental.mp3")
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", inst_wav,
              "-c:a", "libmp3lame", "-b:a", "192k", "-ac", "2", "-ar", "44100", inst_mp3], 600, stage)

        stage = "karaoke video render"; JOBS[kid]["stage"] = stage
        out_mp4 = os.path.join(work, "karaoke.mp4")
        await asyncio.to_thread(_render, work, cover, inst_mp3, ass, out_mp4, width, height, fps, duration, diag)
        size = os.path.getsize(out_mp4)
        if size < 100_000:
            raise RuntimeError("rendered file is suspiciously small")

        stage = "upload"; JOBS[kid]["stage"] = stage
        async with httpx.AsyncClient() as client:
            with open(out_mp4, "rb") as fh:
                r = await client.put(body["upload_video_url"], content=fh.read(),
                                     headers={"Content-Type": "video/mp4"}, timeout=900)
            r.raise_for_status()
            has_inst = has_sub = False
            try:
                with open(inst_mp3, "rb") as fh:
                    r2 = await client.put(body["upload_instrumental_url"], content=fh.read(),
                                          headers={"Content-Type": "audio/mpeg"}, timeout=600)
                has_inst = r2.status_code < 300
                with open(ass, "rb") as fh:
                    r3 = await client.put(body["upload_subtitle_url"], content=fh.read(),
                                          headers={"Content-Type": "text/plain"}, timeout=120)
                has_sub = r3.status_code < 300
            except Exception:  # noqa: BLE001 — extras are optional
                pass

            quality = {
                "vocal_separation_model": DEMUCS_MODEL,
                "alignment_mode": timing["mode"],
                "alignment_confidence": timing["confidence"],
                "lyric_coverage": timing["coverage"],
                "lyrics_source": timing["source"],
                "language": timing["language"],
                "whisper_model": WHISPER_MODEL,
                "device": DEVICE,
            }
            timings = {k: v for k, v in diag.items() if k.endswith("_seconds")}
            timings["total_seconds"] = round(time.time() - diag["started_at"], 1)
            JOBS[kid] = {"status": "COMPLETED", "stage": "done"}
            await _callback(client, body, {
                "job_id": body["job_id"], "callback_token": body["callback_token"],
                "status": "COMPLETED", "duration_seconds": round(duration, 3),
                "width": width, "height": height, "file_size": size,
                "has_instrumental": has_inst, "has_subtitles": has_sub,
                "quality": quality, "timings": timings,
            })
    except Exception as exc:  # noqa: BLE001 — any failure is reported, never charged
        err = _redact(str(exc))[:400]
        diag["exception"] = err
        JOBS[kid] = {"status": "FAILED", "stage": stage, "error": err}
        try:
            async with httpx.AsyncClient() as client:
                await _callback(client, body, {
                    "job_id": body.get("job_id"), "callback_token": body.get("callback_token"),
                    "status": "FAILED", "stage": stage, "error": err,
                    "timings": {k: v for k, v in diag.items() if k.endswith("_seconds")},
                })
        except Exception:  # noqa: BLE001
            pass
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _callback(client: httpx.AsyncClient, body: Dict[str, Any], payload: Dict[str, Any]) -> None:
    raw = json.dumps(payload)
    ts = str(int(time.time()))
    await client.post(body["callback_url"], content=raw,
                      headers={"Content-Type": "application/json", "X-VCN-Timestamp": ts,
                               "X-VCN-Signature": _sign(ts, raw)}, timeout=60)


async def _worker_loop() -> None:
    while True:
        kid, body = await QUEUE.get()
        try:
            await _process(kid, body)
        finally:
            QUEUE.task_done()


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_worker_loop())


# ------------------------------------------------------------------- routes
@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True, "service": "vcn-karaoke-worker", "configured": bool(SECRET),
        "device": DEVICE, "whisper_model": WHISPER_MODEL, "demucs_model": DEMUCS_MODEL,
        "font": bool(_font()), "queue": QUEUE.qsize(),
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }


@app.post("/v1/render-karaoke")
async def render_karaoke(request: Request, x_vcn_timestamp: Optional[str] = Header(None),
                         x_vcn_signature: Optional[str] = Header(None)) -> Dict[str, Any]:
    body = await _verified_body(request, x_vcn_timestamp, x_vcn_signature)
    for f in ("job_id", "audio_url", "upload_video_url", "upload_instrumental_url",
              "upload_subtitle_url", "callback_url", "callback_token"):
        if not body.get(f):
            raise HTTPException(status_code=400, detail=f"Missing {f}")
    kid = f"kar_{uuid.uuid4().hex}"
    JOBS[kid] = {"status": "PENDING", "stage": "queued", "position": QUEUE.qsize()}
    await QUEUE.put((kid, body))
    return {"karaoke_job_id": kid, "status": "PENDING", "queue_position": QUEUE.qsize()}


@app.post("/v1/job-status")
async def job_status(request: Request, x_vcn_timestamp: Optional[str] = Header(None),
                     x_vcn_signature: Optional[str] = Header(None)) -> Dict[str, Any]:
    body = await _verified_body(request, x_vcn_timestamp, x_vcn_signature)
    job = JOBS.get(str(body.get("karaoke_job_id", "")))
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job")
    return job


@app.post("/v1/job-diagnostics")
async def job_diagnostics(request: Request, x_vcn_timestamp: Optional[str] = Header(None),
                          x_vcn_signature: Optional[str] = Header(None)) -> Dict[str, Any]:
    body = await _verified_body(request, x_vcn_timestamp, x_vcn_signature)
    return DIAGNOSTICS.get(str(body.get("karaoke_job_id", "")), {})
