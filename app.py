import json, time, uuid, shutil, subprocess
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse
from PIL import Image, ImageStat
from render_music_video import router as music_video_router

app = FastAPI(title="VCN Media Worker", version="0.6.0")
app.include_router(music_video_router)
WORK_DIR = Path("/tmp/vcn-media"); WORK_DIR.mkdir(parents=True, exist_ok=True)
ASSET_DIR = Path("/app/assets")
LOGO_LIGHT = ASSET_DIR / "vcn-logo-light-bg.png"
LOGO_DARK = ASSET_DIR / "vcn-logo-dark-bg.png"
OUTPUT = WORK_DIR / "vcn-premium-preview.mp4"

def probe(path):
    cmd=["ffprobe","-v","error","-show_entries","format=duration,size,bit_rate:stream=index,codec_name,codec_type,width,height,r_frame_rate,sample_rate,channels","-of","json",str(path)]
    return json.loads(subprocess.check_output(cmd,text=True,timeout=30))

def safe(v):
    return (v or "").strip()[:100].replace("\\","\\\\").replace(":","\\:").replace("'","\\'").replace("%","\\%")

def brightness(path):
    with Image.open(path) as im:
        im=im.convert("RGB"); im.thumbnail((256,256)); r,g,b=ImageStat.Stat(im).mean
        return round(0.2126*r+0.7152*g+0.0722*b,2)

@app.get("/")
def root():
    return {"service":"vcn-media-worker","version":"0.6.0","status":"running"}

@app.get("/health")
def health():
    out=subprocess.check_output(["ffmpeg","-version"],text=True,timeout=10)
    return {"service":"vcn-media-worker","version":"0.6.0","status":"ok","ffmpeg":out.splitlines()[0],"logos":{"light":LOGO_LIGHT.exists(),"dark":LOGO_DARK.exists()}}

@app.post("/render-premium-preview")
async def render(
    audio_file: UploadFile = File(...),
    cover_file: UploadFile = File(...),
    title: str = Form("Untitled"),
    creator_username: str = Form("@vcncreator"),
    preview_seconds: int = Form(15),
    logo_mode: str = Form("auto"),
):
    preview_seconds=max(10,min(int(preview_seconds),20))
    rid=uuid.uuid4().hex[:8]
    ap=WORK_DIR/f"a-{rid}.bin"; cp=WORK_DIR/f"c-{rid}.bin"
    with ap.open("wb") as f: shutil.copyfileobj(audio_file.file,f)
    with cp.open("wb") as f: shutil.copyfileobj(cover_file.file,f)
    ab=brightness(cp)
    if logo_mode=="light": lp,name=LOGO_LIGHT,"light"
    elif logo_mode=="dark": lp,name=LOGO_DARK,"dark"
    elif ab>=135: lp,name=LOGO_LIGHT,"light-auto"
    else: lp,name=LOGO_DARK,"dark-auto"
    if OUTPUT.exists(): OUTPUT.unlink()

    t=safe(title); u=safe(creator_username)
    fc=(
      "color=c=0x06152b:s=1280x720:r=30[bg];"
      "[0:v]scale=760:720:force_original_aspect_ratio=increase,crop=760:720[art];"
      "[bg][art]overlay=0:0[base];"
      "[base]drawbox=x=760:y=0:w=520:h=720:color=0x06152b@0.94:t=fill[p];"
      f"[p]drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='{t}':fontcolor=white:fontsize=42:x=805:y=265,"
      f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='{u}':fontcolor=0x8FE7A9:fontsize=27:x=805:y=325,"
      "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='VCN AI MUSIC STUDIO':fontcolor=white@0.72:fontsize=22:x=805:y=455,"
      "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='Knowledge Creates Opportunities.':fontcolor=white@0.55:fontsize=18:x=805:y=495[d];"
      "[1:v]scale=360:-1[logo];[d][logo]overlay=805:55:format=auto[v]"
    )
    cmd=["ffmpeg","-y","-hide_banner","-loop","1","-i",str(cp),"-loop","1","-i",str(lp),"-i",str(ap),"-t",str(preview_seconds),"-filter_complex",fc,"-map","[v]","-map","2:a:0","-r","30","-c:v","libx264","-preset","ultrafast","-crf","23","-pix_fmt","yuv420p","-threads","2","-c:a","aac","-b:a","160k","-ar","44100","-ac","2","-movflags","+faststart","-shortest",str(OUTPUT)]
    st=time.time(); p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=180); elapsed=round(time.time()-st,2)
    if p.returncode!=0 or not OUTPUT.exists():
        return JSONResponse(status_code=500,content={"status":"failed","version":"0.6.0","ffmpeg_returncode":p.returncode,"render_seconds":elapsed,"stderr_last_lines":p.stderr.splitlines()[-40:]})
    return {"status":"success","version":"0.6.0","render_seconds":elapsed,"cover_brightness":ab,"logo_selected":name,"file_size_bytes":OUTPUT.stat().st_size,"download_path":"/premium-preview.mp4","output_probe":probe(OUTPUT)}

@app.get("/premium-preview.mp4")
def download():
    if not OUTPUT.exists(): raise HTTPException(status_code=404,detail="No premium preview exists yet.")
    return FileResponse(OUTPUT,media_type="video/mp4",filename="vcn-premium-music-video-preview.mp4")
