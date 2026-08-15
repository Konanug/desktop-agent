# Enclosure CAD renders

Media for the project README. Drop the files here under these exact names —
`../../README.md` already references them.

| File | What | Status |
|---|---|---|
| `enclosure-assembled.png` | Isometric, closed. Camera aperture and panel bezel visible. | needed |
| `enclosure-exploded.png`  | Exploded: bezel, panel, Pi 5 + cooler, HAT, rear shell. | needed |
| `enclosure-exploded.mp4`  | ~8 s animated explode. Silent, loops cleanly. | needed |

## Encoding the clip

Keep it small — this is a README, not a download. H.264, no audio, and a width
of 1280 is plenty for an 8-second turntable:

```bash
ffmpeg -i raw-export.mp4 -an -vcodec libx264 -crf 26 -preset slow \
       -pix_fmt yuv420p -vf "scale=1280:-2" -movflags +faststart \
       enclosure-exploded.mp4
```

`-pix_fmt yuv420p` is not optional if you want it to play in every browser, and
`+faststart` puts the index at the front so it begins without downloading the
whole file. Aim for **under 5 MB**.

## Getting it to actually render on github.com

A committed `.mp4` referenced with markdown image syntax **does not** produce a
player — GitHub strips `<video>` from README markdown as well. There is exactly
one path that works:

1. Open any issue or PR comment box on the repo (you do not have to submit it).
2. Drag the `.mp4` in. GitHub uploads it and rewrites it to a
   `https://github.com/user-attachments/assets/…` URL.
3. Copy that URL, cancel the comment, and paste the URL **on its own line** in
   `README.md`, replacing the placeholder.

Commit the file here too. The upload URL is what renders on the web; the
committed copy is what someone gets when they clone, and it is the one that
still exists if the attachment host ever changes.
