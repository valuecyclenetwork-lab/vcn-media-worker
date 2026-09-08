"""
VCN Media Worker — Premium Music Video renderer.

Drop this file next to the existing worker app and mount it:

    from render_music_video import router as music_video_router
    app.include_router(music_video_router)

Requires: ffmpeg + ffprobe on PATH (already present in the Railway image),
fastapi, httpx. Set VCN_MEDIA_WORKER_SECRET to the same value held by VCN.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Any, Dict, Optional

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


def _render(audio: str, cover: str, out: str, title: str, username: str,
            width: int, height: int, fps: int, duration: float, logo: str) -> None:
    """Cinematic cover presentation: blurred bed + slow Ken Burns + branding."""
    frames = max(int(duration * fps), fps)
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
        f"[cfg]scale=2400:-1,zoompan=z='min(zoom+0.00012,1.08)':"
        f"x='iw/2-(iw/zoom/2)+sin(on/{fps*9})*24':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={int(height*0.62)}x{int(height*0.62)}:fps={fps},setsar=1[fg]",
        "[bg][fg]overlay=(W-w)/2:(H-h)/2-40:shortest=1[base]",
        # VCN logo, aspect preserved, safe margin.
        f"[2:v]scale={int(width*0.16)}:-1[logo]",
        f"[base][logo]overlay=W-w-{int(width*0.035)}:{int(height*0.05)}[branded]",
        # Title + exact creator username, faded in over the intro.
        f"[branded]{final_chain}[v]",
    ]

    proc = subprocess.run(
        ["ffmpeg", "-y",
         "-i", audio, "-loop", "1", "-i", cover, "-i", logo,
         "-filter_complex", ";".join(filters),
         "-map", "[v]", "-map", "0:a",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-r", str(fps),
         "-c:a", "aac", "-b:a", "192k", "-ac", "2",
         "-t", f"{duration:.3f}", "-shortest", "-movflags", "+faststart", out],
        capture_output=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("ffmpeg failed: " + " | ".join(tail[-4:]))


async def _run_job(render_job_id: str, body: Dict[str, Any]) -> None:
    work = tempfile.mkdtemp(prefix="vcn-pmv-")
    audio, cover = os.path.join(work, "a.mp3"), os.path.join(work, "c.jpg")
    out = os.path.join(work, "out.mp4")
    try:
        async with httpx.AsyncClient() as client:
            await _download(client, body["audio_url"], audio)
            await _download(client, body["cover_url"], cover)

            duration = _probe_duration(audio)  # full song, never hardcoded
            logo = LOGO_LIGHT if _mean_luma(cover) < 110 else LOGO_DARK

            await asyncio.to_thread(
                _render, audio, cover, out,
                body.get("title") or "Untitled",
                body.get("creator_username") or "",
                int(body.get("width", 1920)), int(body.get("height", 1080)),
                int(body.get("fps", 30)), duration, logo,
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
        JOBS[render_job_id] = {"status": "FAILED", "error": str(exc)[:400]}
        try:
            async with httpx.AsyncClient() as client:
                await _callback(client, body, {
                    "job_id": body.get("job_id"),
                    "callback_token": body.get("callback_token"),
                    "status": "FAILED",
                    "error": str(exc)[:400],
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
