"""Validate, normalise and spool content for the physical panel.

TRUST BOUNDARY
This module is where untrusted bytes stop. Everything risky -- fetching a URL,
decoding an image, resizing -- happens HERE, inside Hermes. The renderer is
handed only raw RGB565 of exactly the expected length, so it never parses a
container format, never touches the network, and never opens a path chosen by
anyone but us. A malicious image cannot reach the process that owns /dev/fb0.

Concretely:
  * only https, and only hosts on an allowlist (Discord's CDN)
  * hard byte cap and connect/read timeout, enforced during streaming so a
    slow-drip or endless response cannot hang or exhaust memory
  * PIL decompression-bomb guard, then verify() then a fresh decode
  * re-encode to raw pixels, which strips EXIF, ICC, and any trailing payload
  * spool bounded by count and total bytes, LRU-evicted
  * TTL so the panel returns to normal on its own

WHY A SEPARATE FILE FROM state.json
The gateway hook owns state.json and rewrites it wholesale from memory. A
second writer would clobber it. Requests therefore go in request.json, and the
renderer reads both.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

# Panel body zone: full width, between the 26px header and 30px footer.
BODY_W, BODY_H = 480, 264

MAX_BYTES = 8 * 1024 * 1024      # refuse anything larger than 8 MiB
FETCH_TIMEOUT = 10.0
MAX_PIXELS = 40_000_000          # decompression-bomb ceiling
SPOOL_MAX_FILES = 8
SPOOL_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_TTL = 60.0
MAX_TTL = 600.0
MAX_TEXT = 120

ALLOWED_HOSTS = {
    "cdn.discordapp.com",
    "media.discordapp.net",
    "images-ext-1.discordapp.net",
    "images-ext-2.discordapp.net",
}


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    d = Path(base) / "hermes-display"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _images_dir() -> Path:
    d = _runtime_dir() / "images"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _err(msg: str, **extra) -> str:
    return json.dumps({"ok": False, "error": msg, **extra})


def _write_request(payload: dict) -> None:
    """Atomically publish the current display request."""
    d = _runtime_dir()
    tmp = d / f".request.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, d / "request.json")


def _prune_spool() -> None:
    """Keep the spool bounded; oldest out first."""
    files = sorted(_images_dir().glob("*.rgb565"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    while files and (len(files) > SPOOL_MAX_FILES or total > SPOOL_MAX_BYTES):
        victim = files.pop(0)
        total -= victim.stat().st_size
        victim.unlink(missing_ok=True)


def _fetch(url: str) -> bytes:
    """Download with host allowlist, timeout and a streaming size cap."""
    from urllib.parse import urlparse

    u = urlparse(url)
    if u.scheme != "https":
        raise ValueError(f"only https is allowed, got {u.scheme!r}")
    if u.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"host {u.hostname!r} is not on the allowlist")

    req = urllib.request.Request(url, headers={"User-Agent": "hermes-pi-display/1.0"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        # Content-Length is a hint from the server, not a guarantee -- check it
        # early to fail fast, but still cap while reading.
        declared = r.headers.get("Content-Length")
        if declared and int(declared) > MAX_BYTES:
            raise ValueError(f"declared {int(declared)} bytes, limit {MAX_BYTES}")
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"larger than the {MAX_BYTES} byte limit")
    return data


def _to_rgb565(raw: bytes) -> bytes:
    """Decode untrusted bytes and return exactly BODY_W*BODY_H*2 bytes.

    Re-encoding to raw pixels is itself the sanitiser: no metadata, no ICC
    profile, no appended payload survives.
    """
    import io

    import numpy as np
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS   # raises DecompressionBombError beyond

    # verify() must run on a fresh handle and invalidates the object, so the
    # image is opened twice: once to validate, once to actually decode.
    Image.open(io.BytesIO(raw)).verify()
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")

    # Letterbox rather than crop or stretch: preserves what was sent.
    canvas = Image.new("RGB", (BODY_W, BODY_H), (0, 0, 0))
    img.thumbnail((BODY_W, BODY_H), Image.LANCZOS)
    canvas.paste(img, ((BODY_W - img.width) // 2, (BODY_H - img.height) // 2))

    a = np.asarray(canvas, dtype=np.uint8)
    r = (a[:, :, 0].astype(np.uint16) >> 3) << 11
    g = (a[:, :, 1].astype(np.uint16) >> 2) << 5
    b = a[:, :, 2].astype(np.uint16) >> 3
    return (r | g | b).astype("<u2").tobytes()


def display_show_image(args: dict, **kwargs) -> str:
    """Tool handler. Always returns JSON; never raises."""
    try:
        url = str(args.get("url") or "").strip()
        if not url:
            return _err("url is required (a Discord attachment/CDN https URL)")

        ttl = float(args.get("seconds") or DEFAULT_TTL)
        ttl = max(5.0, min(ttl, MAX_TTL))

        try:
            raw = _fetch(url)
        except Exception as e:
            return _err(f"could not fetch image: {e}")

        try:
            pixels = _to_rgb565(raw)
        except Exception as e:
            return _err(f"not a usable image: {type(e).__name__}: {e}")

        expected = BODY_W * BODY_H * 2
        if len(pixels) != expected:
            return _err(f"internal size mismatch: {len(pixels)} != {expected}")

        ident = f"{int(time.time()*1000):x}"
        out = _images_dir() / f"{ident}.rgb565"
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(pixels)
        os.replace(tmp, out)
        _prune_spool()

        now = time.time()
        _write_request({
            "schema": 1, "mode": "image", "image": out.name, "text": None,
            "requested_at": now, "expires_at": now + ttl,
            "w": BODY_W, "h": BODY_H,
        })
        return json.dumps({"ok": True, "shown": "image", "seconds": int(ttl),
                           "note": "Displayed on the Pi's panel."})
    except Exception as e:
        return _err(f"unexpected: {type(e).__name__}: {e}")


def display_show_text(args: dict, **kwargs) -> str:
    try:
        text = str(args.get("text") or "").strip()
        if not text:
            return _err("text is required")
        # Collapse to a single printable line: the panel has one strip, and
        # control characters have no business reaching a renderer.
        text = "".join(c for c in text if c.isprintable())[:MAX_TEXT]

        ttl = float(args.get("seconds") or DEFAULT_TTL)
        ttl = max(5.0, min(ttl, MAX_TTL))

        now = time.time()
        _write_request({
            "schema": 1, "mode": "text", "image": None, "text": text,
            "requested_at": now, "expires_at": now + ttl,
        })
        return json.dumps({"ok": True, "shown": "text", "seconds": int(ttl)})
    except Exception as e:
        return _err(f"unexpected: {type(e).__name__}: {e}")


def display_clear(args: dict, **kwargs) -> str:
    try:
        _write_request({"schema": 1, "mode": "idle", "image": None, "text": None,
                        "requested_at": time.time(), "expires_at": None})
        return json.dumps({"ok": True, "shown": "idle"})
    except Exception as e:
        return _err(f"unexpected: {type(e).__name__}: {e}")
