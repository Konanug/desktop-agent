"""Custom hand gestures, learned from your own hands.

WHY LANDMARKS AND NOT IMAGES
The obvious reading of "give it training images" is to fine-tune a vision model
on photographs. That is the wrong tool here and it would be worse at the job:

  * MediaPipe has ALREADY solved the hard part. It turns a photograph into 21
    calibrated 3D points, and it was trained on far more hands than anyone can
    photograph in a kitchen. Training on raw pixels throws that away and starts
    again from lighting, skin tone, sleeve colour and background.
  * Landmarks are INVARIANT to the things that ruin image models. Normalised
    into the hand's own frame (below), the same pose gives the same numbers at
    any distance, any rotation, in any light, for any hand.
  * It is small enough to be honest about. A few hundred samples and a k-NN
    fits in a file you can read, trains in under a second on this Pi, and can
    be deleted and redone in a minute. A fine-tune is an opaque blob you cannot
    inspect and will not retrain casually.

WHAT A SAMPLE IS
21 landmarks, normalised so that only the SHAPE survives:

    1. translate so the wrist is the origin      -- kills position in frame
    2. correct for the frame's aspect            -- kills the portrait-frame
                                                    distortion that made raw
                                                    distances swing 2.6x with
                                                    rotation (see hands._dist)
    3. rotate so wrist->middle-knuckle points up -- kills hand rotation
    4. scale so that vector has length 1         -- kills distance from camera

What is left is a 42-number description of a hand shape, and nothing else.
Two people making the same sign land in nearly the same place.

WHAT THE CLASSIFIER IS
k-NN with a distance ceiling. Deliberately the simplest thing that works, for
one reason that matters more than accuracy: IT CAN SAY "I DO NOT KNOW". A
softmax over N classes always names one of them, and this project has already
been bitten once by a classifier that could not abstain -- classify() used to
name every finger pattern, so a hand in view permanently asserted a command.
Anything past REJECT_DIST is None, and None means no gesture.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np

# Beyond this normalised distance from every stored sample, the answer is None.
# Tuned by tools/gesture_train.py --check against your own held-out samples;
# the default is deliberately tight, because a false command is worse than a
# missed one when anyone in the room can trigger it.
REJECT_DIST = float(os.environ.get("HERMES_CUSTOM_REJECT", "0.34"))

# How many neighbours vote. 3 of 5 within the ceiling, so one stray sample in
# the training set cannot create a phantom gesture.
K = 5
VOTES = 3

WRIST, MIDDLE_MCP = 0, 9


def model_path() -> Path:
    return Path(os.environ.get(
        "HERMES_CUSTOM_MODEL",
        str(Path.home() / ".local/share/hermes-pi/models/custom_gestures.json")))


def normalise(landmarks, aspect: float = 1.0) -> np.ndarray:
    """21 (x, y) -> a 42-vector describing SHAPE ONLY.

    See the module docstring for what each step removes. The order matters:
    aspect before rotation, or the rotation is computed from distorted axes.
    """
    p = np.asarray(landmarks, dtype=np.float64)[:, :2].copy()
    p[:, 1] *= aspect                       # square up the axes
    p -= p[WRIST]                           # wrist to origin

    v = p[MIDDLE_MCP]
    n = math.hypot(v[0], v[1])
    if n < 1e-9:                            # degenerate; nothing to learn from
        return np.zeros(42)

    # Project onto the hand's OWN axes rather than rotating by an angle.
    # Building the basis directly is self-evidently correct -- u is where the
    # hand points, w is across it -- whereas an atan2 plus a rotation matrix
    # has four sign conventions to get right and looks plausible when wrong.
    # The first version here was wrong in exactly that way and normalised the
    # middle knuckle to (-0.75, -0.66) instead of (0, 1).
    u = v / n                               # along wrist -> middle knuckle
    w = np.array([u[1], -u[0]])             # perpendicular to it
    out = np.column_stack([p @ w, p @ u]) / n
    return out.reshape(-1)


class CustomGestures:
    """Learned gestures, loaded from disk. Absent file = feature simply off."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else model_path()
        self.names: list[str] = []
        self._X = np.zeros((0, 42))
        self._y: list[str] = []
        self.error: str | None = None

    @property
    def available(self) -> bool:
        return len(self._y) > 0

    def load(self) -> bool:
        try:
            doc = json.loads(self.path.read_text())
        except OSError:
            self.error = None               # not an error; nothing trained yet
            return False
        except ValueError as e:
            self.error = f"custom gesture model unreadable: {e}"
            return False
        try:
            self._X = np.asarray(doc["samples"], dtype=np.float64)
            self._y = list(doc["labels"])
            self.names = sorted(set(self._y))
            if self._X.shape[0] != len(self._y) or self._X.shape[1] != 42:
                raise ValueError("samples/labels shape mismatch")
        except (KeyError, ValueError) as e:
            self.error = f"custom gesture model malformed: {e}"
            self._X, self._y, self.names = np.zeros((0, 42)), [], []
            return False
        self.error = None
        return True

    def classify(self, landmarks, aspect: float = 1.0) -> tuple[str | None, float]:
        """(name, distance) or (None, distance). None is a real answer."""
        if not self.available:
            return None, float("inf")
        q = normalise(landmarks, aspect)
        d = np.linalg.norm(self._X - q, axis=1)
        order = np.argsort(d)[:K]
        near = [(self._y[i], d[i]) for i in order if d[i] <= REJECT_DIST]
        if len(near) < VOTES:
            return None, float(d[order[0]])
        # Majority among the near neighbours; a tie is not a decision.
        counts: dict[str, int] = {}
        for name, _ in near:
            counts[name] = counts.get(name, 0) + 1
        best, n = max(counts.items(), key=lambda kv: kv[1])
        if n < VOTES or list(counts.values()).count(n) > 1:
            return None, float(d[order[0]])
        return best, float(d[order[0]])
