import json
import time
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI(title="VCN Media Worker", version="0.3.0")
OUTPUT_PATH = Path("/tmp/vcn-render-test.mp4")

def ffmpeg_version():
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-version"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        return out.splitlines()[0] if out else "ffmpeg available"
    except Exception as exc:
        return f"ffmpeg unavailable: {exc}"

def ffprobe_json(path: Path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration,size,bit_rate:"
        "stream=index,codec_name,codec_type,width,height,r_frame_rate,sample_rate,channels",
        "-of", "json",
        str(path),
    ]
    return json.loads(subprocess.check_output(cmd, text=True, timeout=30))

@app.get("/")
def root():
    return {
        "service": "vcn-media-worker",
        "version": "0.3.0",
        "status": "running"
    }

@app.get("/health")
def health():
    version = ffmpeg_version()
    ok = version.startswith("ffmpeg version")
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "service": "vcn-media-worker",
            "version": "0.3.0",
            "status": "ok" if ok else "degraded",
            "ffmpeg": version,
        },
    )

@app.post("/render-test")
def render_test():
    """
    Controlled infrastructure test only.

    v0.3 deliberately removes the showwaves/overlay filter from v0.2.
    The goal is to prove stable 1080p H.264/AAC encoding first.
    """
    start = time.time()

    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()

    vf = (
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "text='VCN AI MUSIC STUDIO':fontcolor=white:fontsize=74:"
        "x=(w-text_w)/2:y=270,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        "text='1080p Media Worker Test':fontcolor=white:fontsize=44:"
        "x=(w-text_w)/2:y=390,"
        "drawbox=x=210:y=570:w=1500:h=12:color=white@0.75:t=fill,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        "text='BUY  -  LEARN  -  LEVERAGE  -  GROW':fontcolor=white:fontsize=34:"
        "x=(w-text_w)/2:y=900"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "color=c=0x071a33:s=1920x1080:r=30",
        "-f", "lavfi",
        "-i", "sine=frequency=220:sample_rate=44100",
        "-t", "10",
        "-vf", vf,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "2",
        "-movflags", "+faststart",
        str(OUTPUT_PATH),
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=504,
            detail={
                "message": "FFmpeg render exceeded 180 seconds.",
                "stderr_tail": (exc.stderr or "")[-5000:] if isinstance(exc.stderr, str) else ""
            },
        )

    if proc.returncode != 0 or not OUTPUT_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail={
                "message": "FFmpeg render failed.",
                "stderr_tail": proc.stderr[-5000:],
            },
        )

    probe = ffprobe_json(OUTPUT_PATH)

    return {
        "service": "vcn-media-worker",
        "version": "0.3.0",
        "test": "controlled-1080p-render",
        "status": "success",
        "render_seconds": round(time.time() - start, 2),
        "file_size_bytes": OUTPUT_PATH.stat().st_size,
        "download_path": "/render-test.mp4",
        "probe": probe,
    }

@app.get("/render-test.mp4")
def download_render_test():
    if not OUTPUT_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail="No test render exists yet. POST /render-test first."
        )
    return FileResponse(
        OUTPUT_PATH,
        media_type="video/mp4",
        filename="vcn-media-worker-render-test.mp4",
    )
