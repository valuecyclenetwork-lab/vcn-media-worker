# VCN Media Worker — v0.3

This version fixes the first controlled render test by removing the
`showwaves + overlay` filter chain that stalled in v0.2.

The test now focuses on the infrastructure proof:

- 1920×1080
- 30 fps
- H.264 / libx264
- AAC stereo
- genuine MP4
- 10 seconds
- basic VCN text/graphics

No VCN production connection, Musicful calls, member data, wallets,
bonuses, karaoke, or permanent storage are involved.
