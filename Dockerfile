FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       ca-certificates \
       fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY render_music_video.py .

RUN mkdir -p /app/assets

COPY vcn-logo-light-bg.png /app/assets/vcn-logo-light-bg.png
COPY vcn-logo-dark-bg.png /app/assets/vcn-logo-dark-bg.png

ENV PORT=8080

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
