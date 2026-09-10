# VCN Karaoke Worker — separate Railway service (vcn-karaoke-worker).
# Models are baked into the image at build time: no live downloads at run time.
#
# Base image is pinned to bookworm (glibc 2.36) on purpose. The floating
# python:3.11-slim tag now resolves to Debian trixie (glibc >= 2.41), whose
# loader rejects the CTranslate2 wheel with:
#   ImportError: libctranslate2...so: cannot enable executable stack as shared
#   object requires: Invalid argument
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TORCH_HOME=/models/torch \
    HF_HOME=/models/hf \
    XDG_CACHE_HOME=/models/cache \
    KARAOKE_DEVICE=cpu \
    KARAOKE_WHISPER_MODEL=small \
    KARAOKE_ALIGN_V2_MODEL=medium \
    KARAOKE_ALIGN_V2_SEPARATOR=dsp

# Runtime dependencies only. PyAV is installed as a prebuilt wheel that bundles
# its own FFmpeg libraries, so no libav*-dev headers and no C compilation are
# needed. The `ffmpeg` binary is still required for the karaoke render step.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      fonts-dejavu-core \
      fonts-liberation \
      libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# CPU-only PyTorch keeps the image ~2.5 GB instead of ~7 GB.
# --only-binary=av,ctranslate2 guarantees neither falls back to a source build.
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu \
      --only-binary=av,ctranslate2 -r requirements.txt

# Build-time smoke test: every heavy import must succeed BEFORE model baking.
# No audio is downloaded or processed here.
RUN python - <<'EOF'
import platform
print("glibc:", platform.libc_ver())
import torch, torchaudio, ctranslate2, transformers, whisperx, demucs
print("torch:", torch.__version__)
print("torchaudio:", torchaudio.__version__)
print("ctranslate2:", ctranslate2.__version__)
print("transformers:", transformers.__version__)
print("demucs:", getattr(demucs, "__version__", "4.0.1"))
from transformers.utils import is_torch_available
assert is_torch_available(), "Transformers disabled PyTorch — version mismatch"
print("imports OK")
EOF

# Bake weights: Demucs htdemucs, faster-whisper "small", English wav2vec2 aligner.
RUN python - <<'EOF'
import torch, demucs.pretrained as dp
dp.get_model("htdemucs")
import whisperx
whisperx.load_model("small", "cpu", compute_type="int8")
whisperx.load_align_model(language_code="en", device="cpu")
# align-v2 recognition model (isolated engine; align-v1 keeps using "small").
whisperx.load_model("medium", "cpu", compute_type="int8")
print("models baked")
EOF

COPY app.py align_v2.py ./
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
