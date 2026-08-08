"""Finger reading, pinned against landmarks MediaPipe actually produced.

The fixtures below are the real 21-point outputs for three hand poses, each at
four rotations, composited into a 540x960 frame -- the stream's actual geometry.
That matters: the camera is mounted rotated, so an upright hand in the room is a
sideways hand in the array, and a finger test that quietly assumes "up" is up
would pass on a laptop webcam and fail here.

Storing landmarks rather than images means this runs on plain python3 with no
mediapipe, no model and no camera, like every other test module here.

MEASUREMENT TRAP, recorded because it cost a wrong conclusion:
feeding unrelated still images to a tracker in RunningMode.VIDEO produces
garbage. VIDEO mode carries a track between frames and uses the previous frame
as a prior, so a jump-cut between different photos breaks it. Measured that way
the classifier scored 9/16 and looked broken; it was the harness that was wrong.
Scored per-clip, with the tracker seeing a steady pose, it is 16/16. If you
re-measure this, give the tracker continuity or use RunningMode.IMAGE.

Run:  python3 tests/test_hands.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camera.hands import (CONNECTIONS, FINGER_NAMES, Hand,   # noqa: E402
                          RESULT_MAX_AGE, Result, classify, fingers_extended)

# (name, expected gesture) -> 21 normalised (x, y) landmarks
FIXTURES = {
    ("thumb_up_rot0", "THUMB"): [(0.6264, 0.5761), (0.622, 0.5181), (0.569, 0.4645), (0.5111, 0.4252), (0.4937, 0.3875), (0.4304, 0.4911), (0.3751, 0.4939), (0.4551, 0.5045), (0.481, 0.5038), (0.4202, 0.5281), (0.3776, 0.5319), (0.4741, 0.5383), (0.4708, 0.5299), (0.4134, 0.5614), (0.3805, 0.5663), (0.4617, 0.5674), (0.4569, 0.5564), (0.409, 0.5929), (0.389, 0.5977), (0.4462, 0.5938), (0.4555, 0.5866)],
    ("thumb_up_rot90", "THUMB"): [(0.6272, 0.4325), (0.5304, 0.4349), (0.4412, 0.4625), (0.3762, 0.4931), (0.3134, 0.5021), (0.4869, 0.5358), (0.4906, 0.5651), (0.5082, 0.5228), (0.5073, 0.5094), (0.5486, 0.5411), (0.5541, 0.5638), (0.5648, 0.5127), (0.5515, 0.515), (0.6041, 0.5446), (0.6112, 0.5623), (0.6135, 0.5195), (0.5959, 0.5224), (0.6567, 0.5467), (0.6641, 0.5577), (0.658, 0.5276), (0.6468, 0.5227)],
    ("thumb_up_rot180", "THUMB"): [(0.3716, 0.4226), (0.3762, 0.4804), (0.4285, 0.5333), (0.4866, 0.5717), (0.5041, 0.6091), (0.5685, 0.5063), (0.6234, 0.5038), (0.5435, 0.4933), (0.5179, 0.4936), (0.5783, 0.4694), (0.6209, 0.4661), (0.5244, 0.4599), (0.5282, 0.4678), (0.5844, 0.4361), (0.6177, 0.4321), (0.5368, 0.4309), (0.5419, 0.4411), (0.5877, 0.4045), (0.609, 0.4004), (0.5519, 0.4043), (0.5425, 0.4108)],
    ("thumb_up_rot270", "THUMB"): [(0.3711, 0.5666), (0.4678, 0.5643), (0.5568, 0.5368), (0.6209, 0.5062), (0.683, 0.4968), (0.5117, 0.463), (0.5075, 0.4338), (0.4896, 0.4761), (0.49, 0.4896), (0.4498, 0.4578), (0.4444, 0.435), (0.4337, 0.486), (0.4469, 0.4839), (0.3941, 0.4544), (0.3872, 0.4367), (0.385, 0.4795), (0.4023, 0.4767), (0.3411, 0.4524), (0.3342, 0.4413), (0.3405, 0.4715), (0.3514, 0.4764)],
    ("victory_rot0", "PEACE"): [(0.521, 0.6427), (0.5883, 0.598), (0.6022, 0.54), (0.5176, 0.514), (0.4358, 0.5051), (0.5432, 0.4777), (0.5613, 0.4172), (0.5664, 0.3788), (0.5692, 0.3435), (0.4721, 0.4815), (0.4523, 0.4132), (0.4385, 0.368), (0.4251, 0.33), (0.4146, 0.5016), (0.3954, 0.4729), (0.4335, 0.5114), (0.4568, 0.5438), (0.3656, 0.5316), (0.3488, 0.5015), (0.3883, 0.527), (0.4152, 0.5535)],
    ("victory_rot90", "PEACE"): [(0.7405, 0.4877), (0.6656, 0.4523), (0.5681, 0.4449), (0.5242, 0.49), (0.509, 0.5336), (0.4629, 0.4763), (0.3615, 0.4668), (0.2969, 0.464), (0.2376, 0.4624), (0.4694, 0.5139), (0.3547, 0.5244), (0.2789, 0.5317), (0.2153, 0.539), (0.5035, 0.5443), (0.455, 0.5546), (0.5192, 0.5346), (0.5735, 0.5224), (0.5543, 0.5702), (0.5035, 0.5792), (0.5461, 0.5583), (0.5908, 0.5442)],
    ("victory_rot180", "PEACE"): [(0.4772, 0.3553), (0.4099, 0.4), (0.396, 0.4579), (0.4809, 0.4839), (0.5627, 0.4927), (0.455, 0.5202), (0.4369, 0.5807), (0.4317, 0.6192), (0.429, 0.6545), (0.526, 0.5164), (0.5458, 0.5848), (0.5597, 0.63), (0.5731, 0.668), (0.5835, 0.4963), (0.6026, 0.525), (0.5646, 0.4866), (0.5414, 0.4542), (0.6323, 0.4663), (0.6493, 0.4964), (0.6098, 0.471), (0.5828, 0.4444)],
    ("victory_rot270", "PEACE"): [(0.2578, 0.5112), (0.3328, 0.5468), (0.4302, 0.5541), (0.4739, 0.5089), (0.4888, 0.4653), (0.5351, 0.5227), (0.6366, 0.5322), (0.7011, 0.535), (0.7605, 0.5365), (0.5286, 0.4851), (0.6434, 0.4746), (0.7193, 0.4672), (0.7831, 0.46), (0.4945, 0.4547), (0.5429, 0.4445), (0.4787, 0.4646), (0.4245, 0.4769), (0.4438, 0.4288), (0.4946, 0.42), (0.452, 0.4409), (0.4073, 0.4551)],
    ("pointing_up_rot0", "POINT"): [(0.484, 0.6139), (0.5346, 0.5769), (0.5648, 0.5283), (0.5201, 0.503), (0.4571, 0.4967), (0.4777, 0.4733), (0.486, 0.4208), (0.4876, 0.3866), (0.4825, 0.3567), (0.416, 0.4878), (0.4372, 0.4619), (0.467, 0.5095), (0.4578, 0.5272), (0.369, 0.5078), (0.3931, 0.4943), (0.4275, 0.5358), (0.4135, 0.5459), (0.3257, 0.5345), (0.3493, 0.5178), (0.3861, 0.5434), (0.3781, 0.5531)],
    ("pointing_up_rot90", "POINT"): [(0.6945, 0.5068), (0.6321, 0.4796), (0.55, 0.4636), (0.5073, 0.4875), (0.4955, 0.5208), (0.4549, 0.5096), (0.366, 0.5057), (0.3071, 0.505), (0.2568, 0.508), (0.4793, 0.5431), (0.4348, 0.5331), (0.5147, 0.5165), (0.5453, 0.5205), (0.5135, 0.5684), (0.4917, 0.556), (0.5613, 0.5372), (0.5782, 0.5442), (0.5588, 0.5917), (0.5311, 0.5794), (0.5749, 0.5594), (0.5908, 0.5634)],
    ("pointing_up_rot180", "POINT"): [(0.5143, 0.384), (0.4637, 0.4211), (0.4336, 0.4697), (0.4781, 0.495), (0.5411, 0.5014), (0.5204, 0.5245), (0.512, 0.577), (0.5105, 0.6112), (0.5156, 0.6412), (0.582, 0.5101), (0.5609, 0.536), (0.5312, 0.4883), (0.5402, 0.4706), (0.6291, 0.4901), (0.605, 0.5037), (0.5707, 0.462), (0.5846, 0.4519), (0.6726, 0.4634), (0.6489, 0.48), (0.6121, 0.4544), (0.6202, 0.4447)],
    ("pointing_up_rot270", "POINT"): [(0.3041, 0.491), (0.3669, 0.5184), (0.449, 0.5342), (0.4914, 0.5102), (0.503, 0.4768), (0.5429, 0.4882), (0.6322, 0.4923), (0.6908, 0.493), (0.7411, 0.4899), (0.5182, 0.455), (0.5625, 0.4655), (0.4823, 0.4819), (0.4523, 0.4773), (0.4841, 0.4297), (0.5068, 0.4421), (0.4368, 0.4609), (0.4203, 0.4535), (0.439, 0.4064), (0.467, 0.4187), (0.4234, 0.4387), (0.4074, 0.4345)],
}


EXPECTED_FINGERS = {
    "THUMB": (1, 0, 0, 0, 0),
    "PEACE": (0, 1, 1, 0, 0),
    "POINT": (0, 1, 0, 0, 0),
}


def test_every_pose_reads_correctly():
    """The whole point: which fingers are up, from real landmarks."""
    wrong = []
    for (name, expect), lm in FIXTURES.items():
        got = classify(fingers_extended(lm))
        if got != expect:
            wrong.append(f"{name}: got {got}, expected {expect}")
    assert not wrong, "misread poses:\n  " + "\n  ".join(wrong)


def test_reading_survives_every_rotation():
    """THE POINT OF THE FIXTURES.

    A hand held the same way must read the same way whichever way round the
    camera is bolted. The finger test measures distance from the wrist rather
    than comparing y coordinates for exactly this reason -- 'tip is above the
    knuckle' is only true for an upright hand, and this sensor is mounted 90
    degrees off.
    """
    by_pose = {}
    for (name, expect), lm in FIXTURES.items():
        by_pose.setdefault(name.rsplit("_rot", 1)[0], []).append(
            (name, classify(fingers_extended(lm))))
    for pose, results in by_pose.items():
        got = {g for _, g in results}
        assert len(got) == 1, (
            f"{pose} read differently depending on rotation: "
            + ", ".join(f"{n}={g}" for n, g in results))


def test_finger_pattern_matches_the_name():
    for (name, expect), lm in FIXTURES.items():
        if expect in EXPECTED_FINGERS:
            got = tuple(int(b) for b in fingers_extended(lm))
            assert got == EXPECTED_FINGERS[expect], (
                f"{name}: fingers {got} != {EXPECTED_FINGERS[expect]}")


def test_unknown_shapes_are_not_gestures_at_all():
    """THE VOCABULARY IS CLOSED, and this is the regression that matters.

    classify() used to fall back to f"{n} UP" for anything unrecognised, so
    EVERY hand pose was a named gesture. A hand is always in some shape, so a
    hand in view permanently asserted a command and moving it fired a run of
    them -- opening a fist passes through four nameable patterns on the way.

    None is the correct answer for "not one of the shapes we act on". It is
    not an error and not a fallback, and the debouncer treats it exactly like
    no hand: nothing to fire, and clear the latch.
    """
    assert classify((0, 1, 0, 1, 0)) is None
    assert classify((1, 1, 1, 0, 0)) is None
    # Removed from the vocabulary ON PURPOSE: these are what a hand passes
    # through while opening, and what a relaxed hand does.
    assert classify((0, 1, 1, 1, 0)) is None, "THREE is a transitional pose"
    assert classify((0, 1, 1, 1, 1)) is None, "FOUR is a transitional pose"
    assert classify((0, 0, 0, 0, 1)) is None, "PINKY is a relaxed hand"
    # ... while the deliberate shapes still resolve.
    assert classify((0, 0, 0, 0, 0)) == "FIST"
    assert classify((1, 1, 1, 1, 1)) == "OPEN"
    assert classify((0, 1, 1, 0, 0)) == "PEACE"


def test_a_human_label_still_exists_for_unrecognised_shapes():
    """Silence downstream, but not silence on the overlay. Someone watching
    the stream still needs to see what their hand is doing, or 'why is nothing
    firing' has no answer."""
    lm = FIXTURES[("victory_rot0", "PEACE")]
    h = Hand(lm, "Right", 0.99)
    assert h.gesture == "PEACE" and h.label == "PEACE"
    h.gesture = None                       # simulate an unrecognised shape
    assert h.label.endswith("UP"), "no caption for an unrecognised hand"


def test_landmark_distances_are_rotation_invariant():
    """MEASURED, and the reason the aspect argument exists.

    Landmarks are normalised by width and height SEPARATELY, so on a portrait
    frame the same physical gap reads differently depending on which way it
    points. Uncorrected, thumb-tip-to-index-tip over hand scale swings
    0.549..1.447 across rotations of the same pose -- a 2.6x spread that no
    fixed threshold survives. Corrected, it holds within 1%.
    """
    from camera.hands import pinch_ratio
    AR = 960 / 540                                   # the real stream frame
    for pose in ("thumb_up", "victory", "pointing_up"):
        vals = [pinch_ratio(lm, AR) for (n, _), lm in FIXTURES.items()
                if n.startswith(pose)]
        assert len(vals) == 4, f"expected 4 rotations of {pose}"
        spread = max(vals) / min(vals)
        assert spread < 1.02, f"{pose} varies {spread:.2f}x with rotation"
        raw = [pinch_ratio(lm, 1.0) for (n, _), lm in FIXTURES.items()
               if n.startswith(pose)]
        if pose == "thumb_up":       # the worst case, kept as the proof
            assert max(raw) / min(raw) > 2.0, \
                "uncorrected distances no longer show the bug this guards"


def test_a_curled_hand_is_not_a_pinch():
    """A fist also brings thumb and index tips together. What separates a
    pinch is that the index tip is still out in front of the hand rather than
    folded back against the palm -- measured 0.87 curled vs 1.84+ extended."""
    from camera.hands import index_reach, PINCH_MIN_REACH
    AR = 960 / 540
    curled = index_reach(FIXTURES[("thumb_up_rot0", "THUMB")], AR)
    extended = index_reach(FIXTURES[("victory_rot0", "PEACE")], AR)
    assert curled < PINCH_MIN_REACH <= extended, \
        f"reach threshold {PINCH_MIN_REACH} does not separate {curled:.2f} "
    assert classify(fingers_extended(FIXTURES[("thumb_up_rot0", "THUMB")]),
                    FIXTURES[("thumb_up_rot0", "THUMB")], AR) == "THUMB", \
        "a curled hand was misread as a pinch"


def test_hand_reports_a_bounding_box_that_contains_every_landmark():
    lm = FIXTURES[("victory_rot0", "PEACE")]
    h = Hand(lm, "Right", 0.99)
    x0, y0, x1, y1 = h.bbox
    for x, y in lm:
        assert x0 <= x <= x1 and y0 <= y <= y1, "landmark outside its own bbox"
    assert 0.0 <= x0 and x1 <= 1.0 and 0.0 <= y0 and y1 <= 1.0


def test_hand_serialises_without_numpy_types():
    """hands.json is read by things outside this project; it must be plain
    JSON, not repr of numpy scalars."""
    import json
    h = Hand(FIXTURES[("point" "ing_up_rot0", "POINT")], "Left", 0.9)
    doc = json.loads(json.dumps(h.as_dict()))
    assert doc["fingers_up"] == 1
    assert doc["gesture"] == "POINT"
    assert set(doc["fingers"]) == set(FINGER_NAMES)
    assert len(doc["landmarks"]) == 21


def test_topology_is_self_consistent():
    """Every connection must reference a landmark that exists, or draw() would
    IndexError on a live frame rather than in a test."""
    for a, b in CONNECTIONS:
        assert 0 <= a < 21 and 0 <= b < 21, f"bad connection {(a, b)}"


# -- staleness ------------------------------------------------------------
def test_a_stale_result_is_not_fresh():
    import time
    lm = FIXTURES[("victory_rot0", "PEACE")]
    now = time.time()
    assert Result([Hand(lm, "Right", 1.0)], now, 60.0).fresh(now)
    old = Result([Hand(lm, "Right", 1.0)], now - RESULT_MAX_AGE - 0.1, 60.0)
    assert not old.fresh(now)


def test_stale_results_are_not_drawn():
    """A box hovering where a hand used to be reads as a live track. Detection
    runs on its own thread and is always somewhat behind, so this is the normal
    case, not an edge case."""
    from PIL import Image
    import time
    from camera import hands as H
    lm = FIXTURES[("victory_rot0", "PEACE")]
    now = time.time()

    fresh_img = Image.new("RGB", (200, 320), (0, 0, 0))
    H.draw(fresh_img, Result([Hand(lm, "Right", 1.0)], now, 60.0), now)
    assert fresh_img.getbbox() is not None, "nothing drawn for a fresh result"

    stale_img = Image.new("RGB", (200, 320), (0, 0, 0))
    H.draw(stale_img, Result([Hand(lm, "Right", 1.0)],
                             now - RESULT_MAX_AGE - 0.5, 60.0), now)
    assert stale_img.getbbox() is None, \
        "a stale hand was drawn -- it reads as a live track"

    none_img = Image.new("RGB", (200, 320), (0, 0, 0))
    H.draw(none_img, None, now)
    assert none_img.getbbox() is None


# -- the overlay must never reach the model -------------------------------
def test_overlay_never_touches_the_frame_hermes_sees():
    """THE ONE THAT MATTERS.

    Everything Hermes sees is built from the raw frame. If the overlay ever
    leaked into it, the model would describe lines this process drew as things
    in the room -- 'the panel never invents state', in a place where it would
    be much harder to notice because the picture would still look plausible.
    """
    import time
    import numpy as np
    from camera import encode, hands as H

    frame = np.zeros((320, 200, 3), dtype=np.uint8)
    before = frame.copy()
    lm = FIXTURES[("victory_rot0", "PEACE")]
    now = time.time()
    result = Result([Hand(lm, "Right", 1.0)], now, 60.0)

    # exactly what _push_stream does
    data = encode.to_stream_jpeg(
        frame, long_edge=320, overlay=lambda im: H.draw(im, result, now))

    assert np.array_equal(frame, before), \
        "the overlay mutated the source frame -- Hermes would see the drawing"
    assert data[:2] == b"\xff\xd8"

    # and the drawing did happen, so the assertion above means something
    from io import BytesIO
    from PIL import Image
    assert np.asarray(Image.open(BytesIO(data))).max() > 40, \
        "nothing was drawn at all, so the isolation check proves nothing"


def test_overlay_is_isolated_even_when_no_resize_happens():
    """The dangerous path. When the frame is already at stream size, PIL's
    Image.fromarray can alias the caller's buffer, so there is no resize to
    accidentally copy it. Missing this would leak the overlay into the ring
    buffer and from there into camera_watch's contact sheet."""
    import time
    import numpy as np
    from camera import encode, hands as H

    frame = np.zeros((320, 200, 3), dtype=np.uint8)
    before = frame.copy()
    lm = FIXTURES[("victory_rot0", "PEACE")]
    now = time.time()
    # long_edge == the frame's own long edge, so _fit_long_edge is a no-op
    encode.to_stream_jpeg(frame, long_edge=320,
                          overlay=lambda im: H.draw(
                              im, Result([Hand(lm, "Right", 1.0)], now, 60.0),
                              now))
    assert np.array_equal(frame, before), \
        "overlay leaked into the source frame when no resize intervened"


# -- custom, user-trained gestures ---------------------------------------
def test_custom_normalisation_removes_position_rotation_and_scale():
    """What a learned sample IS. If this is not exact, every recorded sample
    encodes where the hand happened to be as well as its shape, and the
    classifier learns the room instead of the gesture."""
    import numpy as np
    from camera.custom import normalise
    rng = np.random.default_rng(0)
    base = rng.random((21, 2))
    th = 0.7
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    moved = (base - base[0]) @ R.T * 2.3 + np.array([0.4, 0.2])
    assert np.linalg.norm(normalise(base) - normalise(moved)) < 1e-9, \
        "same pose, moved/rotated/scaled, did not normalise to the same vector"
    # And it must still tell different shapes apart.
    assert np.linalg.norm(normalise(base) - normalise(rng.random((21, 2)))) > 1.0


def test_custom_normalisation_puts_the_hand_in_a_canonical_frame():
    """The invariants the basis projection is supposed to produce. The first
    version used atan2 plus a rotation matrix, looked entirely reasonable, and
    put the middle knuckle at (-0.75, -0.66) instead of (0, 1)."""
    import numpy as np
    from camera.custom import normalise
    out = normalise(np.random.default_rng(1).random((21, 2))).reshape(21, 2)
    assert np.allclose(out[0], [0, 0], atol=1e-9), "wrist not at the origin"
    assert np.allclose(out[9], [0, 1], atol=1e-9), "hand not pointing up at unit scale"


def test_custom_classifier_can_say_it_does_not_know():
    """THE PROPERTY THAT MATTERS. A softmax always names a class; this project
    was already bitten once by a classifier that could not abstain."""
    import numpy as np
    from camera.custom import CustomGestures, normalise
    g = CustomGestures.__new__(CustomGestures)
    rng = np.random.default_rng(2)
    proto = rng.random((21, 2))
    g._X = np.array([normalise(proto + rng.normal(0, 0.002, (21, 2)))
                     for _ in range(10)])
    g._y = ["OK"] * 10
    g.names, g.error = ["OK"], None
    name, _ = g.classify(proto)
    assert name == "OK", "did not recognise the pose it was trained on"
    far, _ = g.classify(rng.random((21, 2)))
    assert far is None, "named a gesture for a hand it has never seen"


def test_built_in_gestures_are_not_shadowed_by_learned_ones():
    """A learned gesture that resembles FIST must not silently take it over --
    bindings that already work would start doing something else with no
    visible cause."""
    import camera.hands as H
    lm = FIXTURES[("victory_rot0", "PEACE")]

    class _Always:
        names = ["ANYTHING"]
        def classify(self, lm, ar=1.0):
            return "ANYTHING", 0.0
    prev = H._CUSTOM
    try:
        H._CUSTOM = _Always()
        assert classify(fingers_extended(lm), lm, 960 / 540) == "PEACE", \
            "a learned gesture shadowed a built-in"
        # ... but it DOES fill the gap where the table says nothing.
        assert classify((0, 1, 0, 1, 0), lm, 960 / 540) == "ANYTHING"
    finally:
        H._CUSTOM = prev


def _run() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:
            fails += 1
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURES'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
