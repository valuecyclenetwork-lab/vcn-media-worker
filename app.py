import json,time,subprocess
from pathlib import Path
from fastapi import FastAPI,HTTPException
from fastapi.responses import JSONResponse,FileResponse
app=FastAPI(title='VCN Media Worker',version='0.3.1')
OUTPUT_PATH=Path('/tmp/vcn-render-test.mp4')

def tail_lines(text,count=40): return text.splitlines()[-count:]
def probe(path):
    if not path.exists() or path.stat().st_size==0: return None
    cmd=['ffprobe','-v','error','-show_entries','format=duration,size,bit_rate:stream=index,codec_name,codec_type,width,height,r_frame_rate,sample_rate,channels','-of','json',str(path)]
    try: return json.loads(subprocess.check_output(cmd,text=True,timeout=30))
    except Exception as e: return {'probe_error':str(e)}

def ffmpeg_version():
    try:
        out=subprocess.check_output(['ffmpeg','-version'],stderr=subprocess.STDOUT,text=True,timeout=10)
        return out.splitlines()[0]
    except Exception as e: return f'ffmpeg unavailable: {e}'

@app.get('/')
def root(): return {'service':'vcn-media-worker','version':'0.3.1','status':'running'}

@app.get('/health')
def health():
    v=ffmpeg_version(); ok=v.startswith('ffmpeg version')
    return JSONResponse(status_code=200 if ok else 503,content={'service':'vcn-media-worker','version':'0.3.1','status':'ok' if ok else 'degraded','ffmpeg':v})

@app.post('/render-test')
def render_test():
    start=time.time()
    if OUTPUT_PATH.exists(): OUTPUT_PATH.unlink()
    vf=("drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='VCN AI MUSIC STUDIO':fontcolor=white:fontsize=74:x=(w-text_w)/2:y=270,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='1080p Media Worker Test':fontcolor=white:fontsize=44:x=(w-text_w)/2:y=390,"
        "drawbox=x=210:y=570:w=1500:h=12:color=white@0.75:t=fill,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='BUY  -  LEARN  -  LEVERAGE  -  GROW':fontcolor=white:fontsize=34:x=(w-text_w)/2:y=900")
    cmd=['ffmpeg','-y','-hide_banner','-stats_period','1','-f','lavfi','-i','color=c=0x071a33:s=1920x1080:r=30','-f','lavfi','-i','sine=frequency=220:sample_rate=44100','-t','10','-vf',vf,'-map','0:v:0','-map','1:a:0','-c:v','libx264','-preset','veryfast','-crf','22','-pix_fmt','yuv420p','-threads','2','-c:a','aac','-b:a','192k','-ar','44100','-ac','2','-movflags','+faststart',str(OUTPUT_PATH)]
    try:
        p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=180)
    except subprocess.TimeoutExpired as e:
        elapsed=round(time.time()-start,2); exists=OUTPUT_PATH.exists(); size=OUTPUT_PATH.stat().st_size if exists else 0
        return JSONResponse(status_code=504,content={'service':'vcn-media-worker','version':'0.3.1','status':'timeout','render_seconds':elapsed,'ffmpeg_returncode':None,'output_exists':exists,'output_size_bytes':size,'partial_probe':probe(OUTPUT_PATH),'stderr_last_lines':tail_lines(e.stderr if isinstance(e.stderr,str) else '',40)})
    elapsed=round(time.time()-start,2); exists=OUTPUT_PATH.exists(); size=OUTPUT_PATH.stat().st_size if exists else 0
    if p.returncode!=0 or not exists:
        return JSONResponse(status_code=500,content={'service':'vcn-media-worker','version':'0.3.1','status':'failed','render_seconds':elapsed,'ffmpeg_returncode':p.returncode,'output_exists':exists,'output_size_bytes':size,'partial_probe':probe(OUTPUT_PATH),'stderr_last_lines':tail_lines(p.stderr,40)})
    return {'service':'vcn-media-worker','version':'0.3.1','test':'controlled-1080p-render','status':'success','render_seconds':elapsed,'ffmpeg_returncode':p.returncode,'file_size_bytes':size,'download_path':'/render-test.mp4','probe':probe(OUTPUT_PATH),'stderr_last_lines':tail_lines(p.stderr,20)}

@app.get('/render-test.mp4')
def download_render_test():
    if not OUTPUT_PATH.exists(): raise HTTPException(status_code=404,detail='No test render exists yet. POST /render-test first.')
    return FileResponse(OUTPUT_PATH,media_type='video/mp4',filename='vcn-media-worker-render-test.mp4')
