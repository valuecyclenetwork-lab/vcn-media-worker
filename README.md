# VCN Media Worker — v0.3.1 Diagnostic

Diagnostic build to identify why the 1080p FFmpeg render exits early.

It reports FFmpeg return code, partial file existence and size, ffprobe output where possible, and the final stderr lines. It also limits encoding to 2 threads for a cleaner infrastructure test.

No VCN production data, Musicful calls, member balances, bonuses, allocations, karaoke, or permanent storage are touched.
