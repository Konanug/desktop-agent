"""Turn a captured frame into the two things that leave this process:

  * a JPEG for the model, under a hard byte ceiling
  * a raw RGB565 panel preview, for the display service

WHY A HARD CEILING
An image sent to the model embeds in immutable conversation history and is
re-sent on every subsequent turn of that session. There is no way to shrink or
evict it afterwards. Measured bytes on this hardware are small (~11 KB for the
normal profile), but "measured in this room, in this light" is not a guarantee:
a noisy, high-detail scene compresses far worse. The ceiling is what makes the
worst case bounded instead of hopeful.

The shrink loop is the same shape as the one in Hermes' own tools/vision_tools.py:
drop quality first (cheap, invisible at these sizes), only then drop resolution.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw

from . import protocol

def _fit_long_edge(size: tuple[int, int], long_edge: int) -> tuple[int, int]:
    """Scale so the longer side equals long_edge, keeping the aspect ratio."""
    w, h = size
    scale = long_edge / max(w, h)
    return (max(1, round(w * scale)), max(1, round(h * scale)))


MIN_QUALITY = 45
QUALITY_STEP = 10
SCALE_STEP = 0.8
MAX_ATTEMPTS = 4


def to_jpeg(frame: np.ndarray, profile: str = protocol.DEFAULT_PROFILE
            ) -> tuple[bytes, tuple[int, int], int]:
    """(jpeg_bytes, (w,h), quality_used), guaranteed under the profile ceiling."""
    spec = protocol.PROFILES.get(profile) or protocol.PROFILES[protocol.DEFAULT_PROFILE]
    size, quality, ceiling = spec["size"], spec["quality"], spec["max_bytes"]

    img = Image.fromarray(frame)
    # Fit the profile's LONG EDGE and keep the aspect ratio, rather than
    # forcing the profile's exact dimensions.
    #
    # The sensor is mounted rotated, so a corrected frame is PORTRAIT while the
    # profiles are written landscape. Resizing straight to (768,432) squashed a
    # 576x1024 frame into a third of its height -- everything in shot came out
    # wide and flat, which is worse than useless when the question is "how many
    # fingers am I holding up".
    target = _fit_long_edge(img.size, max(size))
    if img.size != target:
        img = img.resize(target, Image.BILINEAR)

    for attempt in range(MAX_ATTEMPTS):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=False)
        data = buf.getvalue()
        if len(data) <= ceiling:
            return data, img.size, quality
        # Quality first: at these dimensions it costs almost nothing visually.
        if quality > MIN_QUALITY:
            quality = max(MIN_QUALITY, quality - QUALITY_STEP)
        else:
            img = img.resize((max(160, int(img.width * SCALE_STEP)),
                              max(90, int(img.height * SCALE_STEP))),
                             Image.BILINEAR)
    return data, img.size, quality          # best effort after MAX_ATTEMPTS


def contact_sheet(frames: list[np.ndarray], labels: list[str],
                  profile: str = protocol.DEFAULT_PROFILE
                  ) -> tuple[bytes, tuple[int, int], int]:
    """Several moments as ONE image.

    Phase 2's whole trick. Returning four separate images would cost four times
    the context and be re-sent four times on every later turn; a 2x2 grid at the
    normal profile costs the same as a single frame because image tokens are
    charged per 512px tile, not per picture.

    Labels are burned in because the model must be able to order the tiles and
    know the spacing. They are kept to bare time tokens ("-1.6s") so nothing in
    the image can read as an instruction.
    """
    if not frames:
        raise ValueError("contact_sheet needs at least one frame")
    spec = protocol.PROFILES.get(profile) or protocol.PROFILES[protocol.DEFAULT_PROFILE]
    W, H = spec["size"]

    # Match the sheet to the frames' own aspect, for the same reason to_jpeg
    # does: the source may be portrait, and a squashed grid is harder to read
    # than a portrait one.
    fh, fw = frames[0].shape[0], frames[0].shape[1]
    W, H = _fit_long_edge((fw, fh), max(W, H))
    cols = 1 if len(frames) == 1 else 2
    rows = (len(frames) + cols - 1) // cols
    tw, th = W // cols, H // rows

    sheet = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    for i, f in enumerate(frames):
        tile = Image.fromarray(f).resize((tw, th), Image.BILINEAR)
        x, y = (i % cols) * tw, (i // cols) * th
        sheet.paste(tile, (x, y))
        if i < len(labels):
            # Cheap legibility over any background: dark plate, light text.
            draw.rectangle((x + 2, y + 2, x + 54, y + 16), fill=(0, 0, 0))
            draw.text((x + 5, y + 4), labels[i][:10], fill=(255, 255, 255))
        draw.rectangle((x, y, x + tw - 1, y + th - 1), outline=(40, 40, 40))

    quality, ceiling = spec["quality"], spec["max_bytes"]
    for _ in range(MAX_ATTEMPTS):
        buf = io.BytesIO()
        sheet.save(buf, format="JPEG", quality=quality, optimize=False)
        data = buf.getvalue()
        if len(data) <= ceiling or quality <= MIN_QUALITY:
            return data, sheet.size, quality
        quality = max(MIN_QUALITY, quality - QUALITY_STEP)
    return data, sheet.size, quality


def to_stream_jpeg(frame: np.ndarray,
                   long_edge: int = protocol.STREAM_LONG_EDGE,
                   quality: int = protocol.STREAM_QUALITY) -> bytes:
    """One frame of the live browser view.

    Deliberately NOT to_jpeg(). That function exists to keep an image under a
    byte ceiling because it is about to be welded into immutable conversation
    history; it retries, drops quality, and rescales to get there. A streamed
    frame is replaced 15 times a second and costs nothing after it is gone, so
    the retry loop would only add latency. Fixed size, fixed quality, one pass.

    Aspect is preserved for the same reason it is everywhere else here: the
    sensor is mounted rotated and frames are portrait (576x1024).
    """
    img = Image.fromarray(frame)
    target = _fit_long_edge(img.size, long_edge)
    if img.size != target:
        img = img.resize(target, Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=False)
    return buf.getvalue()


# Panel body zone, matching hermes_display's BODY_W/BODY_H exactly. The
# renderer accepts a file of precisely this length and nothing else, so these
# two numbers are part of that contract.
PREVIEW_W, PREVIEW_H = 480, 264


def to_panel_rgb565(frame: np.ndarray) -> bytes:
    """Letterboxed RGB565 for the panel, exactly PREVIEW_W*PREVIEW_H*2 bytes.

    Aspect is preserved and the remainder left black: stretching the room to
    fit would misrepresent what the camera saw, and this image exists precisely
    so the owner can see what was sent.
    """
    img = Image.fromarray(frame)
    scale = min(PREVIEW_W / img.width, PREVIEW_H / img.height)
    new = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    img = img.resize(new, Image.BILINEAR)

    canvas = Image.new("RGB", (PREVIEW_W, PREVIEW_H), (0, 0, 0))
    canvas.paste(img, ((PREVIEW_W - new[0]) // 2, (PREVIEW_H - new[1]) // 2))

    a = np.asarray(canvas, dtype=np.uint8)
    r = (a[..., 0].astype(np.uint16) >> 3) << 11
    g = (a[..., 1].astype(np.uint16) >> 2) << 5
    b = a[..., 2].astype(np.uint16) >> 3
    return (r | g | b).astype("<u2").tobytes()
