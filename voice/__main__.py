"""hermes-voice entrypoint.

The fourth process. Owns the microphone and nothing else.

WHY A SEPARATE SERVICE, AGAIN
Same reasoning that made the camera its own process. The mic is a resource with
one owner; STT is a multi-second CPU spike that must not land inside the
display's timing-critical loop; and `systemctl --user stop hermes-voice` has to
be a real off switch that does not disturb Discord. Any one of the four can die
without the other three noticing.

THE LOOP
Read 80 ms frames forever. Feed each to the wake word (7.8% of a core,
measured) and into a pre-roll ring. When the phrase fires, keep reading until
the room goes quiet, transcribe what was captured, and hand the text to the
webhook. Then go back to listening.

THE INDICATOR IS NOT DECORATION
status.json says whether the mic is open and whether a turn is being captured,
and the panel reads it. A microphone that is on and does not say so is the same
failure as a camera that is on and does not say so, and this project already
took a position on that. STATE, NEVER CONTENT: no transcript ever enters that
file. What was said is the most sensitive thing here.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

import numpy as np

from . import listen, protocol, sink, speak

POLL_SPEAK = 0.25       # how often to check for something to say
STATUS_PERIOD = 1.0

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    _stop = True


def _atomic_write(path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _num(x, digits: int = 2) -> float:
    """Round AND coerce to a builtin float.

    round(numpy.float32) returns numpy.float32, which json.dumps refuses --
    and it refuses in the status writer, a long way from wherever the value
    came from. onnxruntime hands back float32 scores and numpy hands back
    float32 RMS, so this boundary sees them constantly. Casting once, here, is
    cheaper than remembering at every call site.
    """
    try:
        return round(float(x), digits)
    except (TypeError, ValueError):
        return 0.0


def _muted() -> str | None:
    if protocol.disabled_path().exists():
        return "disabled by owner (~/.config/hermes-pi/voice.disabled)"
    return None


class Service:
    def __init__(self):
        self.rec = listen.Recorder()
        self.wake = listen.WakeWord()
        self.stt = listen.Transcriber()
        self.speaker = speak.Speaker()
        self.limit = sink.RateLimit()
        self.pre = listen.PreRoll()
        self.ends: listen.Endpointer | None = None

        self.state = "starting"     # starting | listening | capturing |
                                    # thinking | speaking | muted | error
        self.started_at = time.time()
        self.heard = 0              # utterances accepted
        self.refused = 0            # rate-limited or too short
        self.last_status = 0.0
        self.last_error: str | None = None
        self.loop_tick = time.monotonic()
        # LIVE DIAGNOSTICS. Without these "it is listening and nothing
        # happens" has no next step: you cannot tell silence at the mic from a
        # wake word that is simply scoring below threshold. Both decay, so
        # they describe the last few seconds rather than the session.
        self.level = 0.0            # current audio RMS
        self.level_peak = 0.0       # loudest recently
        self.wake_peak = 0.0        # highest wake score recently
        self._decay_at = 0.0

    # -- status ---------------------------------------------------------
    def status_doc(self) -> dict:
        """STATE, NEVER CONTENT. No transcript, ever."""
        return {
            "schema": protocol.SCHEMA,
            "updated_at": time.time(),
            "pid": os.getpid(),
            "started_at": self.started_at,
            "state": self.state,
            # The fact the panel needs: is the microphone open right now.
            "mic_open": self.rec.proc is not None,
            "muted": _muted(),
            "wake_word": protocol.WAKE_MODEL,
            "wake_ready": self.wake.available,
            "wake_error": self.wake.error,
            "stt_model": protocol.STT_MODEL,
            "stt_ready": self.stt.available,
            "stt_error": self.stt.error,
            "stt_ms": _num(self.stt.last_ms, 1),
            "tts_ready": self.speaker.available,
            "tts_error": self.speaker.error,
            "heard": self.heard,
            "refused": self.refused,
            "in_last_hour": self.limit.in_last_hour,
            "loop_idle_s": _num(time.monotonic() - self.loop_tick),
            # Is sound arriving, and is it anywhere near the wake word?
            "level": _num(self.level, 1),
            "level_peak": _num(self.level_peak, 1),
            "speech_threshold": _num(self.ends.threshold, 1) if self.ends else None,
            "wake_score": _num(self.wake.score, 3),
            "wake_peak": _num(self.wake_peak, 3),
            "wake_threshold": protocol.WAKE_THRESHOLD,
            "error": self.last_error,
        }

    def publish(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_status < STATUS_PERIOD:
            return
        self.last_status = now
        try:
            _atomic_write(protocol.status_path(),
                          json.dumps(self.status_doc(), indent=2).encode())
        except OSError:
            pass

    # -- capture --------------------------------------------------------
    def capture_utterance(self) -> np.ndarray | None:
        """Everything from the pre-roll until the room goes quiet.

        Bounded by MAX_UTTERANCE so a stuck endpointer records until the disk
        fills rather than forever -- the same instinct as the camera watchdog:
        a failure that is loud and finite beats one that is silent and
        permanent.
        """
        frames = self.pre.drain()
        quiet_for = 0.0
        spoke_for = 0.0
        t0 = time.monotonic()
        per_frame = protocol.FRAME / protocol.RATE

        while time.monotonic() - t0 < protocol.MAX_UTTERANCE:
            f = self.rec.read_frame()
            if f is None:
                return None
            frames.append(f)
            if self.ends is not None and self.ends.is_speech(f):
                quiet_for = 0.0
                spoke_for += per_frame
            else:
                quiet_for += per_frame
                if quiet_for >= protocol.SILENCE_END and spoke_for > 0:
                    break
        if spoke_for < protocol.MIN_UTTERANCE:
            return None                     # a cough, not a request
        return np.concatenate(frames) if frames else None

    def handle_wake(self) -> None:
        self.state = "capturing"
        self.publish(force=True)
        print(f"[voice] wake ({self.wake.score:.2f}) -- listening", flush=True)
        audio = self.capture_utterance()
        self.wake.reset()
        if audio is None:
            print("[voice] nothing usable captured", flush=True)
            self.state = "listening"
            self.publish(force=True)
            return

        secs = len(audio) / protocol.RATE
        self.state = "thinking"
        self.publish(force=True)
        text = self.stt.transcribe(audio)
        # LENGTH AND TIMING ONLY. The transcript is deliberately not logged:
        # journald here is persistent, and a permanent record of everything
        # said near this microphone is not something to create by accident.
        print(f"[voice] {secs:.1f}s audio -> {len(text)} chars "
              f"in {self.stt.last_ms:.0f}ms", flush=True)
        if not text:
            self.state = "listening"
            return

        why = self.limit.check()
        if why is not None:
            self.refused += 1
            print(f"[voice] refused: rate limit ({why})", flush=True)
            self.state = "listening"
            return

        ok, detail = sink.post(text)
        if ok:
            self.limit.record()
            self.heard += 1
            print(f"[voice] delivered to Hermes ({detail})", flush=True)
        else:
            self.last_error = detail
            print(f"[voice] delivery FAILED: {detail}", flush=True)
        self.state = "listening"

    # -- lifecycle ------------------------------------------------------
    def start(self) -> bool:
        why = _muted()
        if why:
            self.state = "muted"
            print(f"[voice] {why}", flush=True)
            return False
        if not self.rec.start():
            self.state = "error"
            self.last_error = self.rec.error
            print(f"[voice] {self.rec.error}", flush=True)
            return False
        if not self.wake.load():
            self.state = "error"
            self.last_error = self.wake.error
            print(f"[voice] {self.wake.error}", flush=True)
            return False
        if not self.stt.load():
            # Not fatal in principle, but a voice service that cannot
            # transcribe has nothing to offer, so say so plainly.
            self.state = "error"
            self.last_error = self.stt.error
            print(f"[voice] {self.stt.error}", flush=True)
            return False
        floor = listen.measure_floor(self.rec)
        self.ends = listen.Endpointer(floor)
        print(f"[voice] room noise floor {floor:.0f}, "
              f"speech threshold {self.ends.threshold:.0f}", flush=True)
        self.state = "listening"
        return True

    def stop(self) -> None:
        self.speaker.stop()
        self.rec.stop()
        self.state = "off"
        self.publish(force=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hermes-voice")
    ap.add_argument("--say", metavar="TEXT",
                    help="speak TEXT and exit (tests the output path)")
    ap.add_argument("--listen-test", action="store_true",
                    help="transcribe one utterance and print it, deliver nothing")
    args = ap.parse_args(argv)

    protocol.ensure_dirs()

    if args.say:
        s = speak.Speaker()
        if not s.available:
            print(f"cannot speak: {s.error}")
            return 1
        s.say(args.say)
        while s.busy:
            time.sleep(0.1)
        return 0

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    svc = Service()
    print(f"[voice] starting: wake={protocol.WAKE_MODEL} "
          f"stt={protocol.STT_MODEL} -> {protocol.WEBHOOK_URL}", flush=True)
    if not svc.start():
        svc.publish(force=True)
        return 1
    svc.publish(force=True)
    print("[voice] listening", flush=True)

    if args.listen_test:
        print("[voice] say the wake word...", flush=True)

    while not _stop:
        svc.loop_tick = time.monotonic()
        frame = svc.rec.read_frame()
        if frame is None:
            svc.last_error = svc.rec.error or "capture stopped"
            print(f"[voice] {svc.last_error} -- exiting for a restart",
                  flush=True)
            break
        svc.pre.push(frame)

        svc.level = listen.frame_rms(frame)
        svc.level_peak = max(svc.level_peak, svc.level)
        svc.wake_peak = max(svc.wake_peak, svc.wake.score)
        now_m = time.monotonic()
        if now_m - svc._decay_at > 5.0:      # "recently" means five seconds
            svc._decay_at = now_m
            svc.level_peak = svc.level
            svc.wake_peak = svc.wake.score

        # NOT WHILE SPEAKING. Otherwise the assistant's own voice goes into the
        # wake detector and it answers itself -- and piper through a speaker in
        # the same room is far louder to this mic than a person is.
        if not svc.speaker.busy and svc.wake.fired(frame):
            svc.handle_wake()

        pending = speak.take_pending()
        if pending and not svc.speaker.busy:
            svc.state = "speaking"
            svc.publish(force=True)
            svc.speaker.say(pending)

        if svc.state == "speaking" and not svc.speaker.busy:
            svc.state = "listening"

        svc.publish()

    svc.stop()
    print("[voice] stopped", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
