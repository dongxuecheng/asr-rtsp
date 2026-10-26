"""Continuous 30 ms speech segmentation, without concatenating distant utterances."""
from array import array
from collections import deque
from dataclasses import dataclass
import datetime as dt
import math
import sys

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2


def pcm_rms(pcm: bytes) -> float:
    samples = array("h", pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return math.sqrt(sum(x * x for x in samples) / len(samples)) if samples else 0.0


@dataclass
class SpeechChunk:
    started_at: dt.datetime
    pcm: bytes

    @property
    def duration(self):
        return len(self.pcm) / (SAMPLE_RATE * 2)


class SpeechSegmenter:
    def __init__(self, max_seconds=45.0, mode=2, threshold=20,
                 min_speech_ms=300, silence_ms=600, padding_ms=300, detector=None):
        if detector is None:
            import webrtcvad
            detector = webrtcvad.Vad(mode)
        self.detector = detector
        self.threshold = threshold
        self.max_frames = max(1, int(max_seconds * 1000 / FRAME_MS))
        self.min_frames = max(1, math.ceil(min_speech_ms / FRAME_MS))
        self.silence_frames = max(1, math.ceil(silence_ms / FRAME_MS))
        self.ring = deque(maxlen=max(1, math.ceil(padding_ms / FRAME_MS)))
        self.frames = []
        self.voiced = 0
        self.silent = 0
        self.active = False
        self.continuing = False

    def feed(self, pcm: bytes, started_at: dt.datetime) -> list[SpeechChunk]:
        if len(pcm) != FRAME_BYTES:
            raise ValueError(f"VAD frame must contain {FRAME_BYTES} bytes")
        voiced = pcm_rms(pcm) >= self.threshold and self.detector.is_speech(pcm, SAMPLE_RATE)
        item = (started_at, pcm, voiced)
        if not self.active:
            self.ring.append(item)
            # Require sustained speech to avoid isolated clicks; include pre-roll.
            if sum(x[2] for x in self.ring) < max(1, math.ceil(self.ring.maxlen * 0.6)):
                return []
            self.frames = list(self.ring)
            self.ring.clear()
            self.voiced = sum(x[2] for x in self.frames)
            self.silent = 0
            self.active = True
        else:
            self.frames.append(item)
            self.voiced += int(voiced)
            self.silent = 0 if voiced else self.silent + 1

        if self.silent >= self.silence_frames:
            return self._emit(continuation=False)
        if len(self.frames) >= self.max_frames:
            return self._emit(continuation=True)
        return []

    def _emit(self, continuation):
        result = []
        if self.frames and (self.voiced >= self.min_frames or (self.continuing and self.voiced > 0)):
            result.append(SpeechChunk(self.frames[0][0], b"".join(x[1] for x in self.frames)))
        self.frames = []
        self.voiced = 0
        self.silent = 0
        self.active = continuation
        self.continuing = continuation and bool(result)
        self.ring.clear()
        return result

    def flush(self):
        return self._emit(continuation=False)
