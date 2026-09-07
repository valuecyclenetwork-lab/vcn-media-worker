import json, time, uuid, shutil, subprocess
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI(title="VCN Media Worker", version="0.5.0")
WORK_DIR = Path("/tmp/vcn-media")
WORK_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = WORK_DIR / "vcn-real-media-test.mp4"

def ffprobe_json(path):
    cmd = ["ffprobe","-v","error","-show_entries",
           "format=duration,size,bit_rate:stream=index,codec_name,codec_type,width,height,r_frame_rate,sample_rate,channels",
           "-of","json",str(path)]
    return json.loads(subprocess.check_output(cmd,text=True,timeout=30))

def safe_text(v):
    return (v or "").strip()[:100].replace("\\","\\\\").replace(":","\\:").replace("'","\\'").replace("%","\\%")

@app.get("/")
def root():
    return {"service":"vcn-media-worker","version":"0.5.0","status":"running"}

@app.get("/health")
def health():
    out = subprocess.check_output(["ffmpeg","-version"],text=True,timeout=10)
    return {"service":"vcn-media-worker","version":"0.5.0","status":"ok","ffmpeg":out.splitlines()[0]}

@app.post("/render-real-test")
async def render_real_test(
    audio_file: UploadFile = File(...),
    cover_file: UploadFile = File(...),
    title: str = Form("Untitled"),
    creator: str = Form("VCN Creator"),
    preview_seconds: int = Form(15),
):
    preview_seconds = max(10, min(int(preview_seconds), 20))
    rid = uuid.uuid4().hex[:10]
    audio_path = WORK_DIR / f"audio-{rid}.bin"
    cover_path = WORK_DIR / f"cover-{rid}.bin"

    with audio_path.open("wb") as f:
        shutil.copyfileobj(audio_file.file, f)
    with cover_path.open("wb") as f:
        shutil.copyfileobj(cover_file.file, f)

    audio_probe = ffprobe_json(audio_path)
    cover_probe = ffprobe_json(cover_path)

    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()

    t = safe_text(title)
    c = safe_text(creator)

    fc = (
        "color=c=0x071a33:s=1280x720:r=30[bg];"
        "[0:v]scale=500:500:force_original_aspect_ratio=decrease,"
        "pad=500:500:(ow-iw)/2:(oh-ih)/2:color=black@0[cover];"
        "[bg][cover]overlay=70:110,"
        "drawbox=x=620:y=120:w=590:h=480:color=black@0.18:t=fill,"
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='{t}':fontcolor=white:fontsize=46:x=660:y=230,"
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='{c}':fontcolor=white@0.88:fontsize=28:x=660:y=300,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='VCN AI MUSIC STUDIO':fontcolor=white@0.75:fontsize=24:x=660:y=430,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='Knowledge Creates Opportunities.':fontcolor=white@0.68:fontsize=20:x=660:y=475[v]"
    )

    cmd = [
        "ffmpeg","-y","-hide_banner",
        "-loop","1","-i",str(cover_path),
        "-i",str(audio_path),
        "-t",str(preview_seconds),
        "-filter_complex",fc,
        "-map","[v]","-map","1:a:0",
        "-r","30","-c:v","libx264","-preset","ultrafast","-crf","24",
        "-pix_fmt","yuv420p","-threads","2",
        "-c:a","aac","-b:a","160k","-ar","44100","-ac","2",
        "-movflags","+faststart","-shortest",str(OUTPUT_PATH)
    ]

    start = time.time()
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
    elapsed = round(time.time()-start,2)

    if proc.returncode != 0 or not OUTPUT_PATH.exists():
        return JSONResponse(status_code=500, content={
            "service":"vcn-media-worker","version":"0.5.0","status":"failed",
            "ffmpeg_returncode":proc.returncode,"render_seconds":elapsed,
            "output_exists":OUTPUT_PATH.exists(),
            "output_size_bytes":OUTPUT_PATH.stat().st_size if OUTPUT_PATH.exists() else 0,
            "stderr_last_lines":proc.stderr.splitlines()[-40:]
        })

    return {
        "service":"vcn-media-worker","version":"0.5.0","status":"success",
        "render_seconds":elapsed,"preview_seconds":preview_seconds,
        "file_size_bytes":OUTPUT_PATH.stat().st_size,
        "download_path":"/real-test.mp4",
        "input_audio_probe":audio_probe,"input_cover_probe":cover_probe,
        "output_probe":ffprobe_json(OUTPUT_PATH)
    }

@app.get("/real-test.mp4")
def download_real_test():
    if not OUTPUT_PATH.exists():
        raise HTTPException(status_code=404, detail="No test render exists yet.")
    return FileResponse(OUTPUT_PATH, media_type="video/mp4", filename="vcn-real-media-test.mp4")
