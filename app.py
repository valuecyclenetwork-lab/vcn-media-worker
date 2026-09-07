import json
import time
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI(title="VCN Media Worker", version="0.2.0")
OUTPUT_PATH = Path("/tmp/vcn-render-test.mp4")

def ffmpeg_version():
    try:
        out = subprocess.check_output(["ffmpeg", "-version"], stderr=subprocess.STDOUT, text=True, timeout=10)
        return out.splitlines()[0] if out else "ffmpeg available"
    except Exception as exc:
        return f"ffmpeg unavailable: {exc}"

def ffprobe_json(path: Path):
    cmd = [
        "ffprobe","-v","error",
        "-show_entries","format=duration,size,bit_rate:stream=index,codec_name,codec_type,width,height,r_frame_rate,sample_rate,channels",
        "-of","json",str(path)
    ]
    return json.loads(subprocess.check_output(cmd, text=True, timeout=30))

@app.get("/")
def root():
    return {"service":"vcn-media-worker","version":"0.2.0","status":"running"}

@app.get("/health")
def health():
    version = ffmpeg_version()
    ok = version.startswith("ffmpeg version")
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"service":"vcn-media-worker","version":"0.2.0","status":"ok" if ok else "degraded","ffmpeg":version}
    )

@app.post("/render-test")
def render_test():
    start = time.time()
    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()

    filter_complex = (
        "[0:v]"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "text='VCN AI MUSIC STUDIO':fontcolor=white:fontsize=74:x=(w-text_w)/2:y=250,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        "text='Premium MP4 Rendering Test':fontcolor=white:fontsize=42:x=(w-text_w)/2:y=355,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        "text='BUY  •  LEARN  •  LEVERAGE  •  GROW':fontcolor=white:fontsize=34:x=(w-text_w)/2:y=920"
        "[bg];"
        "[1:a]showwaves=s=1500x230:mode=line:rate=30:colors=white,format=rgba[wave];"
        "[bg][wave]overlay=(W-w)/2:560:format=auto[v]"
    )

    cmd = [
        "ffmpeg","-y",
        "-f","lavfi","-i","color=c=0x071a33:s=1920x1080:r=30:d=10",
        "-f","lavfi","-i","sine=frequency=220:sample_rate=44100:duration=10",
        "-filter_complex",filter_complex,
        "-map","[v]","-map","1:a",
        "-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p",
        "-c:a","aac","-b:a","192k",
        "-movflags","+faststart","-shortest",
        str(OUTPUT_PATH)
    ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="FFmpeg render exceeded 180 seconds.")

    if proc.returncode != 0 or not OUTPUT_PATH.exists():
        raise HTTPException(status_code=500, detail={"message":"FFmpeg render failed.","stderr_tail":proc.stderr[-5000:]})

    probe = ffprobe_json(OUTPUT_PATH)
    return {
        "service":"vcn-media-worker",
        "test":"controlled-1080p-render",
        "status":"success",
        "render_seconds":round(time.time()-start,2),
        "file_size_bytes":OUTPUT_PATH.stat().st_size,
        "download_path":"/render-test.mp4",
        "probe":probe
    }

@app.get("/render-test.mp4")
def download_render_test():
    if not OUTPUT_PATH.exists():
        raise HTTPException(status_code=404, detail="No test render exists yet. POST /render-test first.")
    return FileResponse(OUTPUT_PATH, media_type="video/mp4", filename="vcn-media-worker-render-test.mp4")
