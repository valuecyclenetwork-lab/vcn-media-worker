"""
VCN Media Worker — Premium Music Video renderer (diagnostic build).

Drop this file next to the existing worker app and mount it:

    from render_music_video import router as music_video_router
    app.include_router(music_video_router)

Requires: ffmpeg + ffprobe on PATH (already present in the Railway image),
fastapi, httpx. Set VCN_MEDIA_WORKER_SECRET to the same value held by VCN.

This build does NOT change the render recipe. It only:
  * captures full FFmpeg diagnostics on failure (sanitised command, return
    code, last 150 stderr lines, matched error lines, input probes, font,
    logo, temp-file sizes);
  * adds a signed POST /v1/diagnose endpoint that runs the EXACT production
    filter graph for the first 10 seconds only and returns the diagnostics
    synchronously (no storage upload, no callback).
Secrets, signatures and signed storage URLs are never emitted.
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
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

router = APIRouter()

SECRET = os.environ.get("VCN_MEDIA_WORKER_SECRET", "")
LOGO_LIGHT = os.environ.get("VCN_LOGO_LIGHT", "assets/vcn-logo-light.png")
LOGO_DARK = os.environ.get("VCN_LOGO_DARK", "assets/vcn-logo.png")


def _pick_font() -> str:
    """First usable bold sans font on this image; drawtext dies without one."""
    candidates = [
        os.environ.get("VCN_FONT", ""),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and "[" not in path and ":" not in path:
            return path
    for root in ("/usr/share/fonts", "/app/assets"):
        for base, _dirs, files in os.walk(root):
            for name in sorted(files):
                if name.lower().endswith((".ttf", ".otf")):
                    full = os.path.join(base, name)
                    if "[" not in full and ":" not in full:
                        return full
    return ""


FONT = _pick_font()
MAX_SKEW = 900

JOBS: Dict[str, Dict[str, Any]] = {}

# Last failure diagnostics, keyed by render job id (in-memory, no secrets).
DIAGNOSTICS: Dict[str, Dict[str, Any]] = {}

ERROR_PATTERNS = re.compile(
    r"Error|error|Invalid|Failed|failed|Cannot|No such|filter|encoder|decoder|"
    r"drawtext|overlay|scale|split|Conversion|Killed|memory|Out of"
)


# ----------------------------------------------------------------- security
def _sign(timestamp: str, raw: str) -> str:
    return hmac.new(
        SECRET.encode(), f"{timestamp}.{raw}".encode(), hashlib.sha256
    ).hexdigest()


async def _verified_body(request: Request, timestamp: Optional[str], signature: Optional[str]):
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


# -------------------------------------------------------------- diagnostics
def _redact(text: str) -> str:
    """Strips anything that could carry a secret, token or signed URL."""
    out = str(text or "")
    out = re.sub(r"https?://\S+", "[redacted-url]", out)
    if SECRET:
        out = out.replace(SECRET, "[redacted-secret]")
    out = re.sub(r"(?i)(token|signature|apikey|api_key|secret)=\S+", r"\1=[redacted]", out)
    return out


def _safe_command(cmd: List[str]) -> List[str]:
    return [_redact(part) for part in cmd]


def _probe(path: str) -> Dict[str, Any]:
    """Full ffprobe summary of a local input file. Never raises."""
    info: Dict[str, Any] = {
        "path": os.path.basename(path),
        "exists": os.path.isfile(path),
        "size_bytes": os.path.getsize(path) if os.path.isfile(path) else 0,
    }
    if not info["exists"]:
        return info
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=60,
        )
        parsed = json.loads(out.stdout or "{}")
        fmt = parsed.get("format", {}) or {}
        info["format_name"] = fmt.get("format_name")
        info["duration"] = fmt.get("duration")
        info["streams"] = [
            {
                "codec_name": s.get("codec_name"),
                "codec_type": s.get("codec_type"),
                "width": s.get("width"),
                "height": s.get("height"),
                "pix_fmt": s.get("pix_fmt"),
                "sample_rate": s.get("sample_rate"),
                "channels": s.get("channels"),
            }
            for s in (parsed.get("streams") or [])
        ]
    except Exception as exc:  # noqa: BLE001
        info["probe_error"] = _redact(str(exc))[:300]
    return info


def _stderr_report(stderr: str) -> Dict[str, Any]:
    lines = [ln.rstrip() for ln in (stderr or "").splitlines() if ln.strip()]
    matched = [ln for ln in lines if ERROR_PATTERNS.search(ln)]
    return {
        "stderr_line_count": len(lines),
        "stderr_tail_150": [_redact(ln) for ln in lines[-150:]],
        "matched_error_lines": [_redact(ln) for ln in matched[-80:]],
    }


# -------------------------------------------------------------------- media
def _probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _mean_luma(path: str) -> float:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf",
         "crop=iw/3:ih/4:iw*2/3:0,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
         "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for line in (out.stderr or "").splitlines():
        if "YAVG" in line and "=" in line:
            try:
                return float(line.rsplit("=", 1)[1])
            except ValueError:
                pass
    return 128.0


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")


async def _download(client: httpx.AsyncClient, url: str, dest: str) -> None:
    async with client.stream("GET", url, follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            async for chunk in r.aiter_bytes(1 << 20):
                fh.write(chunk)


def _filter_graph(width: int, height: int, fps: int, duration: float,
                  title: str, username: str) -> str:
    """The one production filter graph. Identical for renders and diagnostics."""
    fade_out = max(duration - 3.0, 0.1)

    text_layers = []
    if FONT:
        text_layers = [
            f"drawtext=fontfile='{FONT}':text='{_esc(title)}':"
            f"x=(w-text_w)/2:y=h-{int(height*0.16)}:fontsize={int(height*0.058)}:"
            f"fontcolor=white:shadowcolor=black@0.6:shadowx=2:shadowy=2:"
            f"alpha='if(lt(t,0.8),t/0.8,1)'",
            f"drawtext=fontfile='{FONT}':text='{_esc(username)}':"
            f"x=(w-text_w)/2:y=h-{int(height*0.095)}:fontsize={int(height*0.032)}:"
            f"fontcolor=white@0.85:shadowcolor=black@0.6:shadowx=2:shadowy=2:"
            f"alpha='if(lt(t,1.2),max(t-0.4,0)/0.8,1)'",
        ]
    final_chain = ",".join(
        text_layers + [f"fade=t=in:st=0:d=1.2,fade=t=out:st={fade_out:.2f}:d=3"]
    )

    filters = [
        # One decode of the cover, explicitly split: some FFmpeg builds refuse
        # to reuse the same input pad twice in one graph.
        "[1:v]split=2[cbg][cfg]",
        # Blurred, darkened background bed built from the same cover.
        f"[cbg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=40,eq=brightness=-0.22:saturation=0.8,"
        f"setsar=1[bg]",
        # Foreground cover with a slow 8% zoom-in and gentle drift.
        # The cover input is already `-loop 1`, so zoompan emits exactly ONE
        # output frame per incoming frame (d=1) and carries the zoom forward
        # with pzoom instead of buffering a duration-sized frame run.
        f"[cfg]scale=2400:-1,zoompan=z='min(max(zoom,pzoom)+0.00012,1.08)':"
        f"x='iw/2-(iw/zoom/2)+sin(on/{fps*9})*24':y='ih/2-(ih/zoom/2)':"
        f"d=1:s={int(height*0.62)}x{int(height*0.62)}:fps={fps},setsar=1[fg]",
        "[bg][fg]overlay=(W-w)/2:(H-h)/2-40:shortest=1[base]",
        # VCN logo, aspect preserved, safe margin.
        f"[2:v]scale={int(width*0.16)}:-1[logo]",
        f"[base][logo]overlay=W-w-{int(width*0.035)}:{int(height*0.05)}[branded]",
        # Title + exact creator username, faded in over the intro.
        f"[branded]{final_chain}[v]",
    ]
    return ";".join(filters)


def _render(audio: str, cover: str, out: str, title: str, username: str,
            width: int, height: int, fps: int, duration: float, logo: str,
            diagnostics: Optional[Dict[str, Any]] = None) -> None:
    """Cinematic cover presentation: blurred bed + slow Ken Burns + branding."""
    graph = _filter_graph(width, height, fps, duration, title, username)

    cmd = ["ffmpeg", "-y",
           "-i", audio, "-loop", "1", "-i", cover, "-i", logo,
           "-filter_complex", graph,
           "-map", "[v]", "-map", "0:a",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-r", str(fps),
           "-c:a", "aac", "-b:a", "192k", "-ac", "2",
           "-t", f"{duration:.3f}", "-shortest", "-movflags", "+faststart", out]

    if diagnostics is not None:
        diagnostics.update({
            "ffmpeg_command": _safe_command(cmd),
            "filter_graph": graph,
            "font_path": FONT or "(none — text overlay skipped)",
            "logo_path": logo,
            "logo_exists": os.path.isfile(logo),
            "logo_size_bytes": os.path.getsize(logo) if os.path.isfile(logo) else 0,
            "audio_probe": _probe(audio),
            "cover_probe": _probe(cover),
            "requested_duration": round(duration, 3),
            "resolution": f"{width}x{height}",
            "fps": fps,
        })

    started = time.time()
    proc = subprocess.run(cmd, capture_output=True)
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    elapsed = round(time.time() - started, 2)

    if diagnostics is not None:
        diagnostics.update({
            "return_code": proc.returncode,
            "elapsed_seconds": elapsed,
            "killed_by_signal": proc.returncode < 0,
            "output_exists": os.path.isfile(out),
            "output_size_bytes": os.path.getsize(out) if os.path.isfile(out) else 0,
            **_stderr_report(stderr),
        })

    if proc.returncode != 0:
        report = _stderr_report(stderr)
        fatal = report["matched_error_lines"][-4:] or report["stderr_tail_150"][-4:]
        signal_note = (
            f" (process killed by signal {-proc.returncode}; likely out of memory"
            f" or a container limit)" if proc.returncode < 0 else ""
        )
        raise RuntimeError(
            f"ffmpeg failed rc={proc.returncode}{signal_note}: " + " | ".join(fatal)
        )


async def _prepare_inputs(client: httpx.AsyncClient, body: Dict[str, Any], work: str):
    audio = os.path.join(work, "a.mp3")
    cover = os.path.join(work, "c.jpg")
    await _download(client, body["audio_url"], audio)
    await _download(client, body["cover_url"], cover)
    logo = LOGO_LIGHT if _mean_luma(cover) < 110 else LOGO_DARK
    return audio, cover, logo


async def _run_job(render_job_id: str, body: Dict[str, Any]) -> None:
    work = tempfile.mkdtemp(prefix="vcn-pmv-")
    out = os.path.join(work, "out.mp4")
    diagnostics: Dict[str, Any] = {"render_job_id": render_job_id}
    try:
        async with httpx.AsyncClient() as client:
            audio, cover, logo = await _prepare_inputs(client, body, work)
            duration = _probe_duration(audio)  # full song, never hardcoded

            await asyncio.to_thread(
                _render, audio, cover, out,
                body.get("title") or "Untitled",
                body.get("creator_username") or "",
                int(body.get("width", 1920)), int(body.get("height", 1080)),
                int(body.get("fps", 30)), duration, logo, diagnostics,
            )

            size = os.path.getsize(out)
            with open(out, "rb") as fh:
                up = await client.put(
                    body["upload_url"], content=fh.read(),
                    headers={"Content-Type": body.get("upload_content_type", "video/mp4")},
                    timeout=900,
                )
            up.raise_for_status()

            JOBS[render_job_id] = {"status": "COMPLETED"}
            await _callback(client, body, {
                "job_id": body["job_id"],
                "callback_token": body["callback_token"],
                "status": "COMPLETED",
                "duration_seconds": round(duration, 3),
                "width": int(body.get("width", 1920)),
                "height": int(body.get("height", 1080)),
                "file_size": size,
            })
    except Exception as exc:  # noqa: BLE001 — any failure must refund the member
        diagnostics["exception"] = _redact(str(exc))[:600]
        DIAGNOSTICS[render_job_id] = diagnostics
        JOBS[render_job_id] = {"status": "FAILED", "error": _redact(str(exc))[:400]}
        try:
            async with httpx.AsyncClient() as client:
                await _callback(client, body, {
                    "job_id": body.get("job_id"),
                    "callback_token": body.get("callback_token"),
                    "status": "FAILED",
                    "error": _redact(str(exc))[:400],
                })
        except Exception:  # noqa: BLE001
            pass
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _callback(client: httpx.AsyncClient, body: Dict[str, Any], payload: Dict[str, Any]):
    raw = json.dumps(payload)
    ts = str(int(time.time()))
    await client.post(
        body["callback_url"], content=raw,
        headers={
            "Content-Type": "application/json",
            "X-VCN-Timestamp": ts,
            "X-VCN-Signature": _sign(ts, raw),
        },
        timeout=60,
    )


# ------------------------------------------------------------------- routes
@router.post("/v1/render-music-video")
async def render_music_video(
    request: Request,
    x_vcn_timestamp: Optional[str] = Header(None),
    x_vcn_signature: Optional[str] = Header(None),
):
    body = await _verified_body(request, x_vcn_timestamp, x_vcn_signature)
    for field in ("job_id", "audio_url", "cover_url", "upload_url", "callback_url",
                  "callback_token"):
        if not body.get(field):
            raise HTTPException(status_code=400, detail=f"Missing {field}")

    render_job_id = f"pmv_{uuid.uuid4().hex}"
    JOBS[render_job_id] = {"status": "PROCESSING"}
    asyncio.create_task(_run_job(render_job_id, body))
    return {"render_job_id": render_job_id, "status": "PROCESSING"}


@router.post("/v1/job-status")
async def job_status(
    request: Request,
    x_vcn_timestamp: Optional[str] = Header(None),
    x_vcn_signature: Optional[str] = Header(None),
):
    body = await _verified_body(request, x_vcn_timestamp, x_vcn_signature)
    job = JOBS.get(str(body.get("render_job_id", "")))
    if not job:
        return {"status": "UNKNOWN"}
    return {"status": job.get("status", "PROCESSING"), "error": job.get("error", "")}


@router.post("/v1/job-diagnostics")
async def job_diagnostics(
    request: Request,
    x_vcn_timestamp: Optional[str] = Header(None),
    x_vcn_signature: Optional[str] = Header(None),
):
    """Sanitised diagnostics for a previously failed production render."""
    body = await _verified_body(request, x_vcn_timestamp, x_vcn_signature)
    return DIAGNOSTICS.get(str(body.get("render_job_id", "")), {"status": "UNKNOWN"})


@router.post("/v1/diagnose")
async def diagnose(
    request: Request,
    x_vcn_timestamp: Optional[str] = Header(None),
    x_vcn_signature: Optional[str] = Header(None),
):
    """
    Signed diagnostic render: EXACT production filter graph, same inputs, same
    1080p/30 H.264 + AAC settings, but only the first 10 seconds. Nothing is
    uploaded and no callback is sent — the sanitised diagnostics come straight
    back in the response.
    """
    body = await _verified_body(request, x_vcn_timestamp, x_vcn_signature)
    for field in ("audio_url", "cover_url"):
        if not body.get(field):
            raise HTTPException(status_code=400, detail=f"Missing {field}")

    seconds = min(max(float(body.get("seconds", 10)), 1.0), 30.0)
    work = tempfile.mkdtemp(prefix="vcn-diag-")
    out = os.path.join(work, "diag.mp4")
    diagnostics: Dict[str, Any] = {"mode": "diagnostic", "seconds": seconds}
    try:
        async with httpx.AsyncClient() as client:
            audio, cover, logo = await _prepare_inputs(client, body, work)
            diagnostics["source_audio_duration"] = round(_probe_duration(audio), 3)
            try:
                await asyncio.to_thread(
                    _render, audio, cover, out,
                    body.get("title") or "Untitled",
                    body.get("creator_username") or "",
                    int(body.get("width", 1920)), int(body.get("height", 1080)),
                    int(body.get("fps", 30)), seconds, logo, diagnostics,
                )
                diagnostics["result"] = "OK"
            except Exception as exc:  # noqa: BLE001
                diagnostics["result"] = "FFMPEG_FAILED"
                diagnostics["exception"] = _redact(str(exc))[:600]
        return diagnostics
    except Exception as exc:  # noqa: BLE001
        diagnostics["result"] = "SETUP_FAILED"
        diagnostics["exception"] = _redact(str(exc))[:600]
        return diagnostics
    finally:
        shutil.rmtree(work, ignore_errors=True)

