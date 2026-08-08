"""Wake word, endpointing, and transcription.

THE SHAPE, AND WHY
A microphone produces 16 kHz forever. Running speech-to-text on all of it is
not affordable -- base.en costs 2.5x realtime, so continuous transcription
would need 40% of a core doing nothing useful for the 99.9% of the day nobody
is talking to it. So a cheap always-on detector gates an expensive one:

    always   openWakeWord   6.24 ms per 80 ms frame   7.8% of a core (MEASURED)
    on wake  record + VAD   trivial
    then     faster-whisper 2.5x realtime, once per utterance

WHY A RING OF PRE-ROLL
The wake word is only recognised once it has been SAID, so by the time the
detector fires, the first syllable of what follows is already in the past. A
ring of PREROLL seconds is kept at all times and prepended to the recording.
Without it "hey jarvis, what time is it" transcribes as "at time is it".

WHAT THIS MODULE DOES NOT DO
It does not decide whether an utterance should reach the agent, and it does not
speak. It turns sound into text and hands it over. Rate limiting, the panel
indicator and delivery all live in __main__, for the same reason camera/hands.py
tracks and camera/gestures.py decides: the thing that observes should not also
be the thing that acts.
"""

from __future__ import annotations

import collections
import os
import subprocess
import time

import numpy as np

from . import protocol


class Recorder:
    """A long-lived `arecord` reading the HAT, in fixed frames.

    A subprocess rather than a Python ALSA binding because there is no ALSA
    wheel for 3.13 worth adding, and because a pipe that dies is far easier to
    reason about at 3 am than a C extension that wedges. `arecord` is already
    the tool this project uses to test the mics, so the failure modes are
    already familiar.
    """

    def __init__(self, card: str = protocol.CARD, rate: int = protocol.RATE):
        self.card, self.rate = card, rate
        self.proc: subprocess.Popen | None = None
        self.error: str | None = None

    def start(self) -> bool:
        if self.proc is not None:
            return True
        cmd = ["arecord", "-D", self.card, "-f", "S16_LE", "-r", str(self.rate),
               "-c", str(protocol.CHANNELS), "-t", "raw", "-q"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, bufsize=0)
        except OSError as e:
            self.error = f"arecord failed to start: {e}"
            return False
        self.error = None
        return True

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.terminate()
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def read_frame(self) -> np.ndarray | None:
        """Exactly FRAME samples, or None if the pipe died.

        Reads are looped because a pipe read returns what is available, not
        what was asked for. A short read handed to openWakeWord silently
        corrupts its melspectrogram window rather than raising.
        """
        if self.proc is None or self.proc.stdout is None:
            return None
        want = protocol.FRAME * 2          # int16
        buf = b""
        while len(buf) < want:
            chunk = self.proc.stdout.read(want - len(buf))
            if not chunk:
                self.error = "arecord pipe closed"
                return None
            buf += chunk
        return np.frombuffer(buf, dtype=np.int16)


class WakeWord:
    """openWakeWord, loaded lazily. Failure is never fatal to the service."""

    def __init__(self, name: str = protocol.WAKE_MODEL,
                 threshold: float = protocol.WAKE_THRESHOLD):
        self.name, self.threshold = name, threshold
        self.error: str | None = None
        self._model = None
        self.score = 0.0

    def load(self) -> bool:
        if self._model is not None:
            return True
        try:
            import openwakeword
            from openwakeword.model import Model
            import pathlib
            d = pathlib.Path(openwakeword.__file__).parent / "resources" / "models"
            hits = sorted(d.glob(f"{self.name}*.onnx"))
            if not hits:
                have = ", ".join(sorted(p.stem for p in d.glob("*_v*.onnx")))
                self.error = f"wake model {self.name!r} not found (have: {have})"
                return False
            self._model = Model(wakeword_model_paths=[str(hits[0])])
        except Exception as e:
            self.error = (f"openwakeword unavailable ({e.__class__.__name__}: "
                          f"{e}) -- is the service on the voice venv python?")
            return False
        self.error = None
        return True

    @property
    def available(self) -> bool:
        return self._model is not None

    def fired(self, frame: np.ndarray) -> bool:
        """One 80 ms frame in; did the phrase just complete?"""
        if self._model is None:
            return False
        scores = self._model.predict(frame)
        # float(), NOT just max(). onnxruntime hands back numpy float32, which
        # survives round() and then blows up json.dumps at the far end of the
        # program -- in the status writer, nowhere near here.
        # tests/test_hands.py pins the same rule for the camera; this is that
        # lesson arriving late.
        self.score = float(max(scores.values())) if scores else 0.0
        return self.score >= self.threshold

    def reset(self) -> None:
        """After firing, clear the model's internal buffers.

        Without this the same utterance keeps scoring above threshold for
        several more frames and fires again the moment recording stops -- the
        audio equivalent of the level-vs-edge problem that camera/gestures.py
        exists to solve.
        """
        if self._model is not None:
            try:
                self._model.reset()
            except Exception:
                pass


def _rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


class Endpointer:
    """Decide when the person has stopped talking.

    Deliberately an ENERGY threshold calibrated against the room, not a neural
    VAD. Silero would be better at separating speech from noise, but it costs
    another model and another dependency to answer a question that, once the
    wake word has already fired, is nearly always just "has the level dropped".
    The floor is measured from the room at startup rather than hardcoded,
    because a hardcoded threshold is really a statement about someone else's
    room.
    """

    def __init__(self, floor: float):
        self.floor = floor
        # 3x the measured noise floor, with an absolute minimum so a silent
        # room (floor near 0) does not make every rustle count as speech.
        self.threshold = max(floor * 3.0, 120.0)

    def is_speech(self, frame: np.ndarray) -> bool:
        return _rms(frame) > self.threshold


def measure_floor(rec: Recorder, seconds: float = 1.0,
                  discard: float = 0.5) -> float:
    """Sample the room to find out what silence sounds like here.

    THE FIRST FRAMES ARE NOT THE ROOM. arecord hands back whatever was in the
    ALSA buffer when the stream opened, which is silence, so measuring
    immediately reports a floor near zero regardless of how loud the room is --
    observed as `room noise floor 2` on a box that was not that quiet. It only
    makes the endpointer MORE sensitive, so it never blocked anything, but it
    also meant the number printed at startup was not a measurement of anything.
    """
    for _ in range(max(1, int(discard * protocol.RATE / protocol.FRAME))):
        if rec.read_frame() is None:
            return 0.0
    n = max(1, int(seconds * protocol.RATE / protocol.FRAME))
    vals = []
    for _ in range(n):
        f = rec.read_frame()
        if f is None:
            break
        vals.append(_rms(f))
    return float(np.median(vals)) if vals else 0.0


def frame_rms(frame) -> float:
    """Exposed so the service can publish a live level."""
    return _rms(frame)


class Transcriber:
    """faster-whisper, loaded lazily and kept resident."""

    def __init__(self, model: str = protocol.STT_MODEL):
        self.model_name = model
        self.error: str | None = None
        self._m = None
        self.last_ms = 0.0

    def load(self) -> bool:
        if self._m is not None:
            return True
        try:
            from faster_whisper import WhisperModel
            root = os.path.expanduser("~/.local/share/hermes-pi/models/whisper")
            self._m = WhisperModel(self.model_name, device="cpu",
                                   compute_type="int8", download_root=root)
        except Exception as e:
            self.error = f"faster-whisper unavailable: {e}"
            return False
        self.error = None
        return True

    @property
    def available(self) -> bool:
        return self._m is not None

    def transcribe(self, audio: np.ndarray) -> str:
        """int16 samples -> text. Empty string when nothing was said."""
        if self._m is None:
            return ""
        t0 = time.perf_counter()
        pcm = audio.astype(np.float32) / 32768.0
        try:
            # beam_size=1 is greedy. MEASURED: the accuracy difference on short
            # command-style utterances did not justify the latency, and latency
            # is the thing being optimised here.
            segs, _info = self._m.transcribe(pcm, beam_size=1, language="en",
                                             vad_filter=False)
            text = " ".join(s.text for s in segs).strip()
        except Exception as e:                                # pragma: no cover
            self.error = f"transcribe failed: {e}"
            return ""
        self.last_ms = (time.perf_counter() - t0) * 1000
        return text


class PreRoll:
    """The last PREROLL seconds of audio, always."""

    def __init__(self, seconds: float = protocol.PREROLL):
        self.frames = collections.deque(
            maxlen=max(1, int(seconds * protocol.RATE / protocol.FRAME)))

    def push(self, frame: np.ndarray) -> None:
        self.frames.append(frame)

    def drain(self) -> list:
        out = list(self.frames)
        self.frames.clear()
        return out
