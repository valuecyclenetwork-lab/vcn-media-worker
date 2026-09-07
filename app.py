import subprocess
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="VCN Media Worker", version="0.1.0")

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

@app.get("/")
def root():
    return {
        "service": "vcn-media-worker",
        "status": "running",
        "message": "VCN Media Worker is online."
    }

@app.get("/health")
def health():
    version = ffmpeg_version()
    ok = version.startswith("ffmpeg version")
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "service": "vcn-media-worker",
            "status": "ok" if ok else "degraded",
            "ffmpeg": version,
        },
    )
