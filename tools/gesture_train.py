#!/usr/bin/env python3
"""Teach the camera your own hand signs.

    python3 tools/gesture_train.py --record OK --seconds 8
    python3 tools/gesture_train.py --record SPOCK --seconds 8
    python3 tools/gesture_train.py --list
    python3 tools/gesture_train.py --check          # honest accuracy, held out
    python3 tools/gesture_train.py --drop SPOCK

Hold the pose and MOVE IT AROUND while recording -- nearer, further, rotated,
both hands, slightly sloppy. The classifier is invariant to position, rotation
and distance by construction, so those variations cost nothing; what you are
actually collecting is the range of shapes YOU make when you mean that sign,
which is the only thing it cannot derive.

There is no training step to wait for. Samples are the model: k-NN over
normalised landmarks, which is why 8 seconds of holding a pose is enough and
why you can delete a gesture and redo it in a minute.

WHY NOT TRAINING IMAGES
See camera/custom.py. MediaPipe has already turned the photograph into 21
calibrated points; learning from pixels again would re-learn lighting, skin
tone and background instead of shape, need orders of magnitude more data, and
produce something you cannot inspect or delete.

Reads the live /hands.json feed, so the camera service keeps sole ownership of
the sensor. Recording counts as watching the room -- the CAM light is on
throughout, exactly as for the stream.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                           # noqa: E402

from camera import protocol, stream                          # noqa: E402
from camera.custom import (REJECT_DIST, CustomGestures,       # noqa: E402
                           model_path, normalise)


def _url(path: str) -> str:
    port = int(os.environ.get("HERMES_CAMERA_STREAM_PORT",
                              str(protocol.STREAM_PORT)))
    return f"http://127.0.0.1:{port}{path}?k={stream.load_or_create_token()}"


def _keepalive() -> None:
    """Hold a viewer slot so the sensor stays awake and tracking runs."""
    def run():
        while True:
            try:
                with urllib.request.urlopen(_url("/stream.mjpg"), timeout=30) as r:
                    while r.read(65536):
                        pass
            except Exception:
                time.sleep(1.0)
    threading.Thread(target=run, daemon=True).start()


def _poll():
    try:
        with urllib.request.urlopen(_url("/hands.json"), timeout=2) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _load() -> dict:
    try:
        return json.loads(model_path().read_text())
    except (OSError, ValueError):
        return {"samples": [], "labels": []}


def _save(doc: dict) -> None:
    p = model_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, p)


def record(name: str, seconds: float) -> int:
    name = name.strip().upper()
    if not name.isascii() or not name.replace("_", "").isalnum():
        print("name must be plain letters, digits or underscore")
        return 2
    doc = _load()
    print(f"Recording {name}. Hold the pose and move it around "
          f"-- nearer, further, rotated. Starting in 3s...")
    time.sleep(3)

    got, seen, t0 = [], set(), time.time()
    while time.time() - t0 < seconds:
        h = _poll()
        if h and not h.get("stale"):
            for hand in h.get("hands", []):
                stamp = h.get("captured_at")
                if stamp in seen:
                    continue          # same frame, seen through a second hand
                seen.add(stamp)
                lm = hand.get("landmarks")
                if not lm or len(lm) != 21:
                    continue
                got.append(normalise(lm, _aspect(h)).tolist())
        time.sleep(0.08)
        left = seconds - (time.time() - t0)
        print(f"\r  {len(got):4d} samples  {left:4.1f}s left ", end="", flush=True)
    print()

    if len(got) < 20:
        print(f"only {len(got)} samples -- was a hand in view? nothing saved")
        return 1

    # Replace rather than append: re-recording a gesture should CORRECT it, not
    # blend the new attempt with an old bad one.
    keep = [(x, y) for x, y in zip(doc["samples"], doc["labels"]) if y != name]
    doc["samples"] = [x for x, _ in keep] + got
    doc["labels"] = [y for _, y in keep] + [name] * len(got)
    _save(doc)
    print(f"saved {len(got)} samples for {name}  "
          f"({len(set(doc['labels']))} gestures, {len(doc['labels'])} total)")
    print("restart the camera service to use it:  "
          "systemctl --user restart hermes-camera")
    return 0


def _aspect(doc: dict) -> float:
    """Frame aspect the landmarks were normalised against.

    hands.json does not carry it, and the value matters -- see hands._dist.
    The stream is portrait at STREAM_SIZE, so derive it from the same constant
    the service used rather than guessing 1.0.
    """
    w, h = protocol.STREAM_SIZE
    return max(w, h) / min(w, h)


def check() -> int:
    """Held-out accuracy. The only number worth reporting."""
    doc = _load()
    X = np.asarray(doc["samples"], dtype=np.float64)
    y = np.asarray(doc["labels"])
    if len(y) < 40:
        print("not enough samples to say anything honest")
        return 1

    rng = np.random.default_rng(0)
    idx = rng.permutation(len(y))
    cut = int(len(y) * 0.7)
    tr, te = idx[:cut], idx[cut:]

    import camera.custom as cc
    g = CustomGestures.__new__(CustomGestures)
    g._X, g._y = X[tr], list(y[tr])
    g.names = sorted(set(g._y)); g.error = None

    ok = rej = wrong = 0
    for i in te:
        # classify() takes landmarks, but these are already normalised, so go
        # through the same k-NN directly rather than double-normalising.
        d = np.linalg.norm(g._X - X[i], axis=1)
        order = np.argsort(d)[:cc.K]
        near = [g._y[j] for j in order if d[j] <= REJECT_DIST]
        if len(near) < cc.VOTES:
            rej += 1
            continue
        counts = {n: near.count(n) for n in set(near)}
        best, n = max(counts.items(), key=lambda kv: kv[1])
        if n < cc.VOTES or list(counts.values()).count(n) > 1:
            rej += 1
        elif best == y[i]:
            ok += 1
        else:
            wrong += 1

    tot = len(te)
    print(f"held-out on {tot} samples ({len(set(y))} gestures):")
    print(f"  correct  {ok:4d}  ({100*ok/tot:.1f}%)")
    print(f"  WRONG    {wrong:4d}  ({100*wrong/tot:.1f}%)   <- the number that matters")
    print(f"  rejected {rej:4d}  ({100*rej/tot:.1f}%)   (said 'I do not know')")
    print(f"\nreject distance is {REJECT_DIST}. A high WRONG count means gestures")
    print("are too alike; a high rejected count means it is set too tight")
    print("(HERMES_CUSTOM_REJECT). Wrong is worse than rejected here -- anyone")
    print("in the room can trigger these.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gesture_train")
    ap.add_argument("--record", metavar="NAME")
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--drop", metavar="NAME")
    args = ap.parse_args(argv)

    if args.list:
        doc = _load()
        if not doc["labels"]:
            print(f"nothing trained yet ({model_path()})")
            return 0
        for n in sorted(set(doc["labels"])):
            print(f"  {n:16s} {doc['labels'].count(n):4d} samples")
        return 0

    if args.drop:
        doc = _load()
        name = args.drop.strip().upper()
        keep = [(x, y) for x, y in zip(doc["samples"], doc["labels"]) if y != name]
        if len(keep) == len(doc["labels"]):
            print(f"no gesture called {name}")
            return 1
        _save({"samples": [x for x, _ in keep], "labels": [y for _, y in keep]})
        print(f"dropped {name}")
        return 0

    if args.check:
        return check()

    if args.record:
        _keepalive()
        time.sleep(1.5)                 # let the sensor wake and tracking start
        return record(args.record, args.seconds)

    ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(0)
