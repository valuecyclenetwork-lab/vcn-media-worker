# VCN Media Worker — v0.4 Real Media Test

This version moves from synthetic media to one real MP3/audio file + one real cover image.

## Endpoint
`POST /render-real-test`

Multipart fields:
- `audio_file`
- `cover_file`
- `title`
- `creator`
- `preview_seconds` (10–60, default 30)

Output:
- 1920×1080
- H.264 video
- AAC stereo audio
- MP4
- blurred cover background
- centered cover art
- title/creator
- restrained VCN branding

## Important
This is still isolated from VCN production:
- no Musicful API calls;
- no database writes;
- no member charges;
- no bonuses or allocations;
- no permanent storage;
- no karaoke.
