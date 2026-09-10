# VCN Karaoke Worker — separate Railway service (vcn-karaoke-worker).
# Models are baked into the image at build time: no live downloads at run time.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TORCH_HOME=/models/torch \
    HF_HOME=/models/hf \
    XDG_CACHE_HOME=/models/cache \
    KARAOKE_DEVICE=cpu \
    KARAOKE_WHISPER_MODEL=small

# Install system build dependencies required by PyAV, Demucs, WhisperX, and FFmpeg.
RUN apt-get update && apt-get install -y --no-install-recommends \
      pkg-config \
      build-essential \
      python3-dev \
      ffmpeg \
      libavformat-dev \
      libavcodec-dev \
      libavdevice-dev \
      libavutil-dev \
      libavfilter-dev \
      libswscale-dev \
      libswresample-dev \
      fonts-dejavu-core \
      fonts-liberation \
      libsndfile1 \
      git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# CPU-only PyTorch keeps the image ~2.5 GB instead of ~7 GB.
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# Bake weights: Demucs htdemucs, faster-whisper "small", English wav2vec2 aligner.
RUN python - <<'EOF'
import torch, demucs.pretrained as dp
dp.get_model("htdemucs")
import whisperx
whisperx.load_model("small", "cpu", compute_type="int8")
whisperx.load_align_model(language_code="en", device="cpu")
print("models baked")
EOF

COPY app.py .
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
