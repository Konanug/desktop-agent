"""Hand and finger tracking, off the capture loop.

MediaPipe's hand landmarker: 21 points per hand, handedness, up to two hands.
Proof of concept for gesture control -- it TRACKS and DRAWS, and drives
absolutely nothing. See "This triggers nothing" below.

WHY MEDIAPIPE, WHEN CLAUDE.md SAID NOT TO
That instruction was written when there was no wheel: pip-only, uncertain
Python 3.13/aarch64 support, PEP 668. Re-checked 2026-08-05 and the situation
had changed -- mediapipe 1.0.0 publishes py3-none-manylinux_2_28_aarch64, which
installs cleanly. It lives in its own venv created with --system-site-packages
so picamera2 (apt, not pip-installable) stays visible, and VERIFIED not to
shadow the system numpy: the camera service depends on numpy 2.2.4 and
picamera2 breaks against a different one.

The model does NOT ship in the wheel -- zero .tflite files -- so
scripts/install-cv.sh fetches hand_landmarker.task separately.

WHY A WORKER THREAD
MEASURED on this Pi: inference is ~60 ms and, notably, ~60 ms at EVERY input
size (480x270 60.1 ms, 960x540 62.9 ms) because mediapipe rescales internally
to its own fixed input. Two consequences that shape this file:

  * Resolution is nearly free for detection, so the tracker is handed the same
    frame the stream already produced. It costs no extra capture, no extra
    resize, and no extra copy.
  * 60 ms is two thirds of a core at 10 Hz, and it CANNOT run inline. The
    capture loop paces a 15 fps stream; blocking it for 60 ms would drop the
    stream to 16 fps-equivalent stutter. So detection runs on its own thread at
    its own rate and the stream draws the most recent result.

Which means results ARE stale by construction, and staleness is handled the way
everything else in this project handles it: a result older than RESULT_MAX_AGE
is not drawn at all, rather than drawn over a hand that has since moved.

THIS TRIGGERS NOTHING
No gesture here is wired to any action, and that is deliberate rather than
unfinished. A gesture trigger is a path from "someone waves in the room" to
"the agent runs a tool", and the Discord allowlist does not cover that path at
all -- anyone physically present would become an unauthenticated user of a bot
that has a shell. docs/SECURITY.md has the design that would be needed first.
This module publishes an observation. Deciding it means something is a separate
job with a separate threat model.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

# -- MediaPipe hand landmark topology ------------------------------------
# 0 wrist; 1-4 thumb; 5-8 index; 9-12 middle; 13-16 ring; 17-20 pinky.
WRIST = 0
CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),              # thumb
    (0, 5), (5, 6), (6, 7), (7, 8),              # index
    (5, 9), (9, 10), (10, 11), (11, 12),         # middle
    (9, 13), (13, 14), (14, 15), (15, 16),       # ring
    (13, 17), (17, 18), (18, 19), (19, 20),      # pinky
    (0, 17),                                     # palm edge
)
TIPS = (4, 8, 12, 16, 20)
FINGER_NAMES = ("thumb", "index", "middle", "ring", "pinky")

# (pip, tip) per finger, used by the extension test below.
_PIP_TIP = ((3, 4), (6, 8), (10, 12), (14, 16), (18, 20))
_PINKY_MCP = 17

# A result older than this is not drawn. A box over where a hand USED to be is
# worse than no box: it reads as a live track.
RESULT_MAX_AGE = 0.5

DEFAULT_MODEL = Path.home() / ".local/share/hermes-pi/models/hand_landmarker.task"


def model_path() -> Path:
    return Path(os.environ.get("HERMES_CAMERA_HAND_MODEL", str(DEFAULT_MODEL)))


# Landmark indices used by the shape tests below.
_MIDDLE_MCP = 9         # wrist->here is the hand's own scale reference
_THUMB_TIP, _INDEX_TIP = 4, 8


# -- gesture reading ------------------------------------------------------
def _dist(a, b, ar: float = 1.0) -> float:
    """Distance between two landmarks, in units proportional to PIXELS.

    `ar` is the frame's height/width. It is not optional decoration.

    MediaPipe normalises x by frame WIDTH and y by frame HEIGHT, separately.
    On this camera the frame is portrait (540x960), so a horizontal 0.1 is 54
    px and a vertical 0.1 is 96 px -- the same physical gap reads differently
    depending on which way it points, and therefore on which way the hand is
    turned.

    MEASURED on the twelve real fixtures in tests/test_hands.py, thumb-tip to
    index-tip over hand scale:

        pose        raw (ar=1)      aspect-corrected
        thumb_up    0.549 .. 1.447      0.924 .. 0.932
        victory     1.030 .. 1.247      1.089 .. 1.092
        pointing    0.993 .. 1.098      1.067 .. 1.071

    Raw swings by 2.6x across rotations of the SAME pose. Corrected, it is
    stable to under 1%. Any threshold on a raw distance is really a threshold
    on how the hand happens to be oriented.

    fingers_extended() below survives without this because it compares two
    distances measured in nearly the same DIRECTION, so the distortion mostly
    cancels. Anything comparing distances in different directions -- a pinch,
    which is thumb-to-index against wrist-to-knuckle -- does not get that.
    """
    dx = a[0] - b[0]
    dy = (a[1] - b[1]) * ar
    return (dx * dx + dy * dy) ** 0.5


def hand_scale(lm, ar: float = 1.0) -> float:
    """Wrist to middle knuckle: the hand's own ruler.

    Every shape test is a RATIO against this, so it works at any distance from
    the camera without ever needing to know the distance.
    """
    return max(1e-6, _dist(lm[WRIST], lm[_MIDDLE_MCP], ar))


def pinch_ratio(lm, ar: float = 1.0) -> float:
    """Thumb tip to index tip, over hand scale. Small means pinched."""
    return _dist(lm[_THUMB_TIP], lm[_INDEX_TIP], ar) / hand_scale(lm, ar)


def index_reach(lm, ar: float = 1.0) -> float:
    """Index tip to wrist, over hand scale. Distinguishes a PINCH from a FIST.

    Both bring the thumb and index tips close together. What differs is where
    the index tip IS: out in front of the hand when pinching, folded back
    against the palm when fisted. Measured on the fixtures, a curled hand
    (thumb_up) reads 0.87 while extended fingers read 1.84-1.96.
    """
    return _dist(lm[_INDEX_TIP], lm[WRIST], ar) / hand_scale(lm, ar)


def fingers_extended(lm) -> tuple[bool, ...]:
    """Which of the five fingers are extended, from 21 normalised landmarks.

    Measured against the WRIST rather than by comparing y coordinates, which is
    the usual shortcut. "Tip is above the knuckle" only works for an upright
    hand, and this camera is mounted rotated and produces portrait frames, so
    an upright hand in the room is a sideways hand in the array. Distance from
    the wrist is invariant to how the hand is turned: an extended finger puts
    its tip further from the wrist than its middle joint, a curled one does
    not.

    The thumb needs a different test because it folds ACROSS the palm rather
    than toward the wrist -- a curled thumb barely changes its wrist distance.
    It is measured against the pinky knuckle instead, which it moves toward.

    VALIDATED, not reasoned: three poses at four rotations each, composited
    into a real 540x960 stream frame, 16/16 correct. A metric-3D variant using
    hand_world_landmarks was also tried and scored WORSE (14/16), so the
    normalised coordinates stay despite x and y being scaled by width and
    height separately.

    Beware when re-measuring: feeding unrelated still images to a tracker in
    RunningMode.VIDEO produces garbage, because VIDEO carries a track between
    frames and uses the previous one as a prior. Measured that way this scored
    9/16 and looked broken. Give it a steady clip, or use RunningMode.IMAGE.
    """
    out = []
    for i, (pip, tip) in enumerate(_PIP_TIP):
        if i == 0:
            ref = lm[_PINKY_MCP]
            out.append(_dist(lm[tip], ref) > _dist(lm[pip], ref))
        else:
            out.append(_dist(lm[tip], lm[WRIST]) > _dist(lm[pip], lm[WRIST]))
    return tuple(out)


# THE VOCABULARY IS CLOSED, AND THAT IS THE POINT.
#
# This used to fall back to f"{n} UP" for any unrecognised finger pattern, so
# EVERY hand pose resolved to some gesture. A hand is always in some shape, so
# a hand in view was permanently asserting a command, and simply moving it
# through the air fired a run of them: opening a fist passes through (0,1,0,0,0),
# (0,1,1,0,0), (0,1,1,1,0), (0,1,1,1,1) on the way to OPEN, and each one was a
# nameable "gesture" that something downstream would act on.
#
# Only shapes in here produce an event. Everything else is None, which means
# exactly what "no hand" means to the debouncer: nothing to act on, and clear
# the latch. Transitional poses on the way between two real gestures fall in
# the gap and are silent.
#
# Kept deliberately small. THREE and FOUR were removed precisely because they
# are what a hand passes through while opening; PINKY because it is what a
# relaxed hand does. Every name here is a shape you have to mean.
_GESTURES = {
    (0, 0, 0, 0, 0): "FIST",
    (1, 1, 1, 1, 1): "OPEN",
    (0, 1, 0, 0, 0): "POINT",
    (0, 1, 1, 0, 0): "PEACE",
    (1, 0, 0, 0, 0): "THUMB",
    (1, 0, 0, 0, 1): "CALL",
    (1, 1, 0, 0, 1): "ROCK",
}

# Shapes that cannot be read off five booleans and need the landmarks. PINCH is
# checked BEFORE the table, because a pinching hand's finger pattern is
# ambiguous -- the index is curled enough that the extension test can go either
# way -- and the thumb-to-index distance is not.
#
# Thresholds are PROVISIONAL. tools/gesture_calibrate.py prints these two
# numbers live so they can be set from a real hand at a real distance rather
# than from my arithmetic. Measured non-pinch baseline on the twelve fixtures:
# pinch ratio 0.92-1.09, index reach 0.87 (curled) vs 1.84-1.96 (extended).
PINCH_MAX = float(os.environ.get("HERMES_PINCH_MAX", "0.45"))
PINCH_MIN_REACH = float(os.environ.get("HERMES_PINCH_MIN_REACH", "1.10"))


# Learned gestures, loaded once. None until load_custom() is called, so the
# feature costs nothing at all when nothing has been trained.
_CUSTOM = None


def load_custom():
    """Load user-trained gestures, if any. Never fatal."""
    global _CUSTOM
    from .custom import CustomGestures
    g = CustomGestures()
    g.load()
    _CUSTOM = g if g.available else None
    return _CUSTOM


def classify(fingers, lm=None, ar: float = 1.0) -> str | None:
    """A name from the CLOSED vocabulary, or None. Never a guess.

    None is a real answer meaning "this hand is not making one of the gestures
    we act on" -- it is not an error and not a fallback.
    """
    if lm is not None:
        # A fist also brings thumb and index tips together; what separates a
        # pinch is that the index tip is still out in front of the hand rather
        # than folded back against the palm.
        if (pinch_ratio(lm, ar) < PINCH_MAX
                and index_reach(lm, ar) >= PINCH_MIN_REACH):
            return "PINCH"

    built_in = _GESTURES.get(tuple(int(b) for b in fingers))
    if built_in is not None or lm is None or _CUSTOM is None:
        # BUILT-INS WIN. A learned gesture that happens to look like FIST must
        # not silently shadow it -- existing bindings on the laptop would start
        # doing something else with no visible cause. Learned gestures fill the
        # gap where the finger table says nothing, which is exactly the space
        # they were recorded in.
        return built_in
    name, _dist = _CUSTOM.classify(lm, ar)
    return name


def vocabulary() -> set[str]:
    """Which gestures may fire, narrowed by HERMES_CAMERA_GESTURE_SET.

    An owner who only wants PINCH and PEACE to do anything should not have to
    bind the rest to nothing on every client -- and narrowing here also stops
    the unwanted ones consuming the rate limit.
    """
    raw = os.environ.get("HERMES_CAMERA_GESTURE_SET", "").strip()
    if not raw:
        return set(_GESTURES.values()) | {"PINCH"}
    return {w.strip().upper() for w in raw.split(",") if w.strip()}


class Hand:
    __slots__ = ("landmarks", "handedness", "score", "fingers", "count",
                 "gesture", "bbox", "aspect", "pinch", "reach")

    def __init__(self, landmarks, handedness: str, score: float,
                 aspect: float = 1.0):
        self.landmarks = landmarks          # 21 x (x, y) normalised 0..1
        self.handedness = handedness
        self.score = score
        self.aspect = aspect                # frame height/width -- see _dist
        self.fingers = fingers_extended(landmarks)
        self.count = sum(self.fingers)
        self.pinch = pinch_ratio(landmarks, aspect)
        self.reach = index_reach(landmarks, aspect)
        # May be None: a hand not making a vocabulary gesture is a normal,
        # frequent, correct outcome.
        self.gesture = classify(self.fingers, landmarks, aspect)
        xs = [p[0] for p in landmarks]
        ys = [p[1] for p in landmarks]
        self.bbox = (min(xs), min(ys), max(xs), max(ys))

    @property
    def label(self) -> str:
        """For HUMANS to read on the overlay -- never for deciding anything.

        An unrecognised shape still deserves a useful caption, so this falls
        back to a finger count. classify() deliberately does NOT, because that
        fallback is what made every pose look like a command.
        """
        return self.gesture or f"{self.count} UP"

    def as_dict(self) -> dict:
        return {
            "handedness": self.handedness,
            "score": round(self.score, 3),
            "gesture": self.gesture,           # null when not in the vocabulary
            "label": self.label,
            "fingers_up": self.count,
            "fingers": {n: bool(f) for n, f in zip(FINGER_NAMES, self.fingers)},
            # The two ratios the shape tests turn on, published so
            # tools/gesture_calibrate.py can set thresholds from a real hand.
            "pinch_ratio": round(self.pinch, 3),
            "index_reach": round(self.reach, 3),
            "bbox": [round(v, 4) for v in self.bbox],
            "landmarks": [[round(x, 4), round(y, 4)] for x, y in self.landmarks],
        }


class Result:
    __slots__ = ("hands", "at", "latency")

    def __init__(self, hands, at: float, latency: float):
        self.hands = hands
        self.at = at                # wall clock of the FRAME, not of the answer
        self.latency = latency

    def fresh(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.at <= RESULT_MAX_AGE


class HandTracker:
    """Detection on its own thread. Never blocks the capture loop."""

    def __init__(self, interval: float = 0.10, max_hands: int = 2):
        self.interval = interval
        self.max_hands = max_hands
        self.error: str | None = None
        self._lm = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._pending = None                    # (frame, wall) -- newest only
        self._result: Result | None = None
        self._ts = 0                            # mediapipe wants increasing ms
        self.frames = 0
        self.mean_ms = 0.0

    # -- lifecycle ------------------------------------------------------
    def load(self) -> bool:
        """Import and load the model. Failure is NEVER fatal.

        Hermes' ability to see through this camera is the service's primary
        job; hand tracking is an addition to it. If the venv is missing, the
        model was never fetched, or mediapipe cannot initialise, the camera
        must carry on doing everything else.
        """
        if self._lm is not None:
            return True
        path = model_path()
        if not path.exists():
            self.error = (f"hand model not found at {path} "
                          f"-- run scripts/install-cv.sh")
            return False
        try:
            from mediapipe.tasks import python as mpp
            from mediapipe.tasks.python import vision
        except Exception as e:
            self.error = (f"mediapipe unavailable ({e.__class__.__name__}: {e})"
                          f" -- is the service running the cv-venv python?")
            return False
        try:
            self._vision = vision
            import mediapipe as mp
            self._mp = mp
            self._lm = vision.HandLandmarker.create_from_options(
                vision.HandLandmarkerOptions(
                    base_options=mpp.BaseOptions(model_asset_path=str(path)),
                    # VIDEO, not IMAGE: it carries a track between frames, so
                    # the expensive palm detector only reruns when a hand is
                    # lost rather than on every frame.
                    running_mode=vision.RunningMode.VIDEO,
                    num_hands=self.max_hands,
                    min_hand_detection_confidence=0.5,
                    min_hand_presence_confidence=0.5,
                    min_tracking_confidence=0.5))
        except Exception as e:
            self.error = f"hand model failed to load: {e}"
            self._lm = None
            return False
        self.error = None
        return True

    @property
    def available(self) -> bool:
        return self._lm is not None

    def start(self) -> bool:
        if self._thread is not None:
            return True
        if not self.load():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hands",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=2.0)
        self._result = None

    # -- producer side --------------------------------------------------
    def offer(self, frame, wall: float) -> None:
        """Hand over the newest frame. Non-blocking, and drops the old one.

        Queueing would be wrong here. If detection falls behind, the useful
        thing is the CURRENT hand position, not a backlog of where it was.
        """
        with self._cond:
            self._pending = (frame, wall)
            self._cond.notify()

    def result(self) -> Result | None:
        return self._result

    # -- worker ---------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                while self._pending is None and not self._stop.is_set():
                    self._cond.wait(0.5)
                if self._stop.is_set():
                    return
                frame, wall = self._pending
                self._pending = None
            t0 = time.perf_counter()
            try:
                image = self._mp.Image(
                    image_format=self._mp.ImageFormat.SRGB, data=frame)
                self._ts += max(1, int(self.interval * 1000))
                res = self._lm.detect_for_video(image, self._ts)
            except Exception as e:                          # pragma: no cover
                self.error = f"detect failed: {e}"
                time.sleep(0.5)
                continue
            ms = (time.perf_counter() - t0) * 1000

            # Frame aspect, so landmark distances can be made isotropic. The
            # frame is portrait here and normalised coordinates are not.
            try:
                aspect = frame.shape[0] / frame.shape[1]
            except (AttributeError, IndexError, ZeroDivisionError):
                aspect = 1.0

            hands = []
            for i, marks in enumerate(res.hand_landmarks):
                cat = res.handedness[i][0] if i < len(res.handedness) else None
                hands.append(Hand(
                    [(p.x, p.y) for p in marks],
                    # MediaPipe labels handedness as if looking AT the person;
                    # passed through as given rather than reinterpreted, since
                    # guessing which way the camera is facing would be worse.
                    getattr(cat, "category_name", "?"),
                    getattr(cat, "score", 0.0),
                    aspect))
            self._result = Result(hands, wall, ms)
            self.frames += 1
            self.mean_ms += (ms - self.mean_ms) / min(self.frames, 30)


# -- overlay --------------------------------------------------------------
# Drawn ONLY on the stream copy. See draw() for why that matters.
_CYAN = (0, 230, 255)
_WHITE = (235, 255, 255)
_TIP = (255, 190, 60)
_BOX = (0, 200, 225)


def draw(img, result: Result | None, now: float | None = None) -> None:
    """Paint landmarks, skeleton and a bounding box onto a PIL image, in place.

    THE FRAME PASSED HERE IS A STREAM-ONLY COPY, and it has to stay that way.
    Everything Hermes sees comes from the untouched frame: if the overlay ever
    reached camera_look, the model would be reasoning about lines this code
    drew and reporting them as things in the room. That is the "panel never
    invents state" rule wearing different clothes, and it would be much harder
    to notice here because the picture would look plausible.

    Nothing is drawn for a stale result -- a box hovering where a hand used to
    be reads as a live track.
    """
    if result is None or not result.fresh(now):
        return
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    W, H = img.size

    for hand in result.hands:
        px = [(x * W, y * H) for x, y in hand.landmarks]

        x0, y0, x1, y1 = hand.bbox
        box = (x0 * W - 8, y0 * H - 8, x1 * W + 8, y1 * H + 8)
        d.rectangle(box, outline=_BOX, width=2)

        for a, b in CONNECTIONS:
            d.line((px[a], px[b]), fill=_CYAN, width=2)

        for i, (x, y) in enumerate(px):
            r = 4 if i in TIPS else 3
            d.ellipse((x - r, y - r, x + r, y + r),
                      fill=_TIP if i in TIPS else _WHITE)

        # .label, not .gesture: an unrecognised shape still gets a caption for
        # the person watching. Only .gesture reaches the debouncer.
        mark = "" if hand.gesture else "  ~"
        label = (f"{hand.handedness.upper()}  {hand.label}  "
                 f"[{hand.count}]{mark}")
        ly = max(0, box[1] - 15)
        d.rectangle((box[0], ly, box[0] + 7.2 * len(label) + 8, ly + 14),
                    fill=(0, 0, 0))
        d.text((box[0] + 4, ly + 3), label, fill=_CYAN)
