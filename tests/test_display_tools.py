"""Hostile-input tests for the display plugin.

display_show_image fetches a URL the model was told about by a Discord
message, so the URL is attacker-influenceable in the general case. This module
is the trust boundary: it must refuse everything except a real image from an
allowlisted host, because whatever it emits is blitted by the process that owns
/dev/fb0.

Run:  python3 tests/test_display_tools.py
"""
import io
import json
import os
import sys

from PIL import Image

try:
    import pytest
except ImportError:      # pytest is not installed system-wide on the Pi;
    pytest = None        # the __main__ runner below covers the same cases.

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "hermes_ext", "plugins"))
from hermes_display import tools  # noqa: E402


REJECT_URLS = [
    ("plain http",              "http://cdn.discordapp.com/x.png"),
    ("arbitrary host",          "https://evil.example.com/x.png"),
    ("SSRF to loopback",        "https://127.0.0.1/x.png"),
    ("SSRF to cloud metadata",  "https://169.254.169.254/latest/meta-data"),
    ("file scheme",             "file:///etc/passwd"),
    # Suffix match would pass this; the check is on the full hostname.
    ("allowlist lookalike",     "https://cdn.discordapp.com.evil.com/x.png"),
    ("empty",                   ""),
]


def test_urls_rejected():
    for name, url in REJECT_URLS:
        assert json.loads(tools.display_show_image({"url": url})).get("ok") is False, name


def test_non_images_rejected():
    for raw in (b"\x00\x01\x02" * 400,
                b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
                b"<html><body>hi</body></html>"):
        try:
            tools._to_rgb565(raw)
            raise AssertionError(f"accepted non-image: {raw[:12]!r}")
        except AssertionError:
            raise
        except Exception:
            pass          # expected


def test_decompression_bomb_rejected():
    """A ~400 KiB file that decodes to 144M pixels. Without the guard this
    allocates hundreds of MB on a Pi that is also running the agent."""
    buf = io.BytesIO()
    Image.new("RGB", (12000, 12000), (1, 2, 3)).save(buf, "PNG")
    try:
        tools._to_rgb565(buf.getvalue())
        raise AssertionError("decompression bomb was accepted")
    except AssertionError:
        raise
    except Exception:
        pass              # expected: DecompressionBombError


def test_valid_image_normalised_to_exact_size():
    """Output must be EXACTLY the expected byte count: the renderer trusts the
    length and reshapes without checking dimensions itself."""
    buf = io.BytesIO()
    Image.new("RGB", (900, 500), (10, 200, 220)).save(buf, "PNG")
    px = tools._to_rgb565(buf.getvalue())
    assert len(px) == tools.BODY_W * tools.BODY_H * 2


def test_text_sanitised():
    # Redirect the runtime dir to a temp path. The real one is
    # /run/user/<uid>, which exists only for a logged-in user -- a CI runner
    # has none and cannot create it, so this passed on the Pi and failed on
    # every runner. A test should not require the machine it was written on.
    import pathlib as _p, tempfile
    d = _p.Path(tempfile.mkdtemp())
    tools._runtime_dir = lambda: d

    tools.display_show_text({"text": "hello\x00\x1b[31m world" + "x" * 400})
    got = json.loads((tools._runtime_dir() / "request.json").read_text())["text"]
    assert "\x00" not in got and "\x1b" not in got
    assert len(got) <= tools.MAX_TEXT
    tools.display_clear({})


if __name__ == "__main__":
    fails = 0
    for name, url in REJECT_URLS:
        ok = json.loads(tools.display_show_image({"url": url})).get("ok") is False
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  reject {name}")
    for fn in (test_non_images_rejected, test_decompression_bomb_rejected,
               test_valid_image_normalised_to_exact_size, test_text_sanitised):
        try:
            fn(); print(f"  PASS  {fn.__name__}")
        except Exception as e:
            fails += 1; print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURES'}")
    sys.exit(1 if fails else 0)
