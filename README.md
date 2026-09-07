# VCN Media Worker — v0.2

Controlled Railway/FFmpeg infrastructure test.

- `GET /health` checks FFmpeg.
- `POST /render-test` generates one synthetic 10-second 1920x1080 H.264/AAC MP4.
- `GET /render-test.mp4` downloads the generated test.

No VCN production data, Musicful calls, wallets, bonuses, database writes, karaoke, or permanent storage are involved.
