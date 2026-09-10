# vcn-karaoke-worker

Separate Railway service that produces the **Karaoke Version** included in the
₦1,000 VCN Music Package. It is independent from the Full HD Music Video worker
(`../render_music_video.py`), which must not be changed.

## What it does
1. Downloads the member's private source MP3 (signed VCN link) + cover art.
2. **Demucs `htdemucs`** — removes the vocals of *this exact song* (instrumental + vocals stems).
3. **WhisperX forced alignment** — the stored VCN lyrics are the authoritative text
   and are aligned word-by-word against the vocals stem. Only when a song has no
   stored lyrics does it transcribe (flagged `lyrics_source = transcribed`).
4. Writes an **ASS** subtitle file: line highlight by default; word-by-word karaoke
   fill only when confidence ≥ 0.6 and ≥ 85 % of words were timed.
5. FFmpeg renders 1920×1080 / 30 fps H.264 + AAC stereo with the instrumental.
6. Uploads MP4 + instrumental MP3 + `.ass` to signed VCN storage URLs and POSTs a
   signed callback. Every failure is reported to VCN; VCN never charges for Karaoke.

## Deploy on Railway (new service, own repo or `media-worker/karaoke` root)
- Build from the `Dockerfile` in this folder (models are baked at build time —
  first build takes 10–20 minutes and the image is ~3–4 GB).
- Plan: **8 GB RAM** minimum, 1 replica, no autoscaling (jobs run one at a time).
- Variables:
  - `VCN_KARAOKE_WORKER_SECRET` — new random secret (also store it in VCN as
    `VCN_KARAOKE_WORKER_SECRET`).
  - `KARAOKE_WHISPER_MODEL=small` (upgrade to `medium` if RAM allows; better Pidgin).
  - `KARAOKE_DEVICE=cpu`
- Health check path: `/health`.

## VCN side
- `VCN_KARAOKE_WORKER_URL` = the Railway public URL of this service.
- `VCN_KARAOKE_WORKER_SECRET` = the same secret.
- Feature flag `karaoke_package` (Admin → Platform settings) turns automatic
  Karaoke on for new songs; staff can always queue one song manually from
  Admin → AI Music Studio → Karaoke Versions.

## Endpoints
| Method | Path | Auth |
| --- | --- | --- |
| GET | `/health` | none |
| POST | `/v1/render-karaoke` | HMAC |
| POST | `/v1/job-status` | HMAC |
| POST | `/v1/job-diagnostics` | HMAC |

HMAC: `X-VCN-Timestamp` + `X-VCN-Signature` = HMAC-SHA256(secret, `"<ts>.<raw body>"`), 900 s skew.

## Expected timings (CPU, 3–4 min song)
Demucs 3–6 min · ASR + alignment 1–3 min · render 1–2 min → **6–12 min**.
Peak RAM ≈ 5–6 GB. Temp disk ≈ 300–600 MB per job (cleaned after each job).
