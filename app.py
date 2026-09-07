import json
import time
import uuid
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI(title="VCN Media Worker", version="0.4.0")

WORK_DIR = Path("/tmp/vcn-media")
WORK_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = WORK_DIR / "vcn-real-media-test.mp4"

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
    out = subprocess.check_output(cmd, text=True, timeout=30)
    return json.loads(out)

def safe_text(value: str) -> str:
    # Keep test input simple/safe for FFmpeg drawtext.
    value = (value or "").strip()[:100]
    return (
        value.replace("\\", "\\\\")
             .replace(":", "\\:")
             .replace("'", "\\'")
             .replace("%", "\\%")
    )

@app.get("/")
def root():
    return {
        "service": "vcn-media-worker",
        "version": "0.4.0",
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
            "version": "0.4.0",
            "status": "ok" if ok else "degraded",
            "ffmpeg": version,
        },
    )

@app.post("/render-real-test")
async def render_real_test(
    audio_file: UploadFile = File(...),
    cover_file: UploadFile = File(...),
    title: str = Form("Untitled"),
    creator: str = Form("VCN Creator"),
    preview_seconds: int = Form(30),
):
    """
    Controlled REAL-MEDIA test.

    Upload one MP3/audio file + one cover image.
    Produces a 1920x1080 H.264/AAC MP4 preview using real media.
    No VCN production integration or database writes.
    """
    preview_seconds = max(10, min(int(preview_seconds), 60))
    request_id = uuid.uuid4().hex[:10]
    audio_path = WORK_DIR / f"audio-{request_id}.bin"
    cover_path = WORK_DIR / f"cover-{request_id}.bin"

    with audio_path.open("wb") as f:
        shutil.copyfileobj(audio_file.file, f)
    with cover_path.open("wb") as f:
        shutil.copyfileobj(cover_file.file, f)

    try:
        audio_probe = ffprobe_json(audio_path)
        cover_probe = ffprobe_json(cover_path)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Uploaded media could not be probed: {exc}"
        )

    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()

    safe_title = safe_text(title)
    safe_creator = safe_text(creator)

    # Premium-style composition:
    # - image fills 1920x1080 as blurred background
    # - same image appears centered in a clean square card
    # - title + creator below
    # - restrained VCN branding
    filter_complex = (
        "[0:v]"
        "scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,"
        "boxblur=25:8,"
        "eq=brightness=-0.18:saturation=1.15"
        "[bg];"
        "[0:v]"
        "scale=720:720:force_original_aspect_ratio=decrease,"
        "pad=720:720:(ow-iw)/2:(oh-ih)/2:color=black@0"
        "[cover];"
        "[bg][cover]"
        "overlay=(W-w)/2:110,"
        "drawbox=x=520:y=90:w=880:h=920:color=black@0.24:t=fill,"
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        f"text='{safe_title}':fontcolor=white:fontsize=58:"
        "x=(w-text_w)/2:y=855,"
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        f"text='{safe_creator}':fontcolor=white@0.9:fontsize=34:"
        "x=(w-text_w)/2:y=925,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "text='VCN AI MUSIC STUDIO':fontcolor=white@0.78:fontsize=28:"
        "x=70:y=70,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        "text='Knowledge Creates Opportunities.':fontcolor=white@0.70:fontsize=24:"
        "x=70:y=1030"
        "[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", str(cover_path),
        "-i", str(audio_path),
        "-t", str(preview_seconds),
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "1:a:0",
        "-r", "30",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "21",
        "-pix_fmt", "yuv420p",
        "-threads", "2",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "2",
        "-movflags", "+faststart",
        "-shortest",
        str(OUTPUT_PATH),
    ]

    start = time.time()
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=240,
    )
    elapsed = round(time.time() - start, 2)

    if proc.returncode != 0 or not OUTPUT_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Real-media FFmpeg render failed.",
                "ffmpeg_returncode": proc.returncode,
                "render_seconds": elapsed,
                "stderr_last_lines": proc.stderr.splitlines()[-40:],
            },
        )

    output_probe = ffprobe_json(OUTPUT_PATH)

    return {
        "service": "vcn-media-worker",
        "version": "0.4.0",
        "test": "real-media-1080p-preview",
        "status": "success",
        "render_seconds": elapsed,
        "preview_seconds": preview_seconds,
        "file_size_bytes": OUTPUT_PATH.stat().st_size,
        "download_path": "/real-test.mp4",
        "input_audio_probe": audio_probe,
        "input_cover_probe": cover_probe,
        "output_probe": output_probe,
    }

@app.get("/real-test.mp4")
def download_real_test():
    if not OUTPUT_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail="No real-media test render exists yet. POST /render-real-test first."
        )
    return FileResponse(
        OUTPUT_PATH,
        media_type="video/mp4",
        filename="vcn-real-media-test.mp4",
    )
