"""Device-free E2E for the Deepgram incremental branch of stream_tts_to_speaker.

Real function, real _StreamerPlayback, real threads; ONLY the audio device and the
provider resolution seam are faked. Nothing is played aloud.
"""
import queue
import sys
import threading

sys.path.insert(0, r"C:/Users/tenta/AppData/Local/hermes/hermes-agent")

import tools.tts_streaming as ts
import tools.tts_tool_speaker as sp

CHUNK_BYTES = 240 * 2  # 10 ms @24k mono int16


class FakePortAudio:
    def __init__(self):
        self.frames = 0
        self.started = False

    def start(self):
        self.started = True

    def write(self, arr):
        self.frames += len(arr)

    def stop(self):
        pass

    def close(self):
        pass


device = FakePortAudio()
sp._StreamerPlayback._create_output_stream = lambda self: device


class FakeIncremental:
    """Mirrors the real DeepgramStreamer's contract on the incremental surface."""

    sample_rate = 24000
    channels = 1
    sample_width = 2
    supports_streaming = True

    def __init__(self):
        self.calls = []
        self._q = queue.Queue()
        self._session_done = threading.Event()

    def open_session(self):
        self.calls.append("open")

    def feed(self, text_delta):
        self.calls.append(("feed", text_delta))
        self._q.put(b"\x01\x00" * 240)

    def iter_audio(self, timeout=0.0):
        if timeout:
            try:
                yield self._q.get(timeout=timeout)
            except queue.Empty:
                return
        while True:
            try:
                yield self._q.get_nowait()
            except queue.Empty:
                return

    def flush(self):
        self.calls.append("flush")
        self._session_done.set()

    def close_session(self):
        self.calls.append("close")


fake = FakeIncremental()
ts.resolve_streaming_provider = lambda cfg, preferred=None: fake

Deltas = ["The compass ", "holds steady ", "when the ", "stars get loud."]
text_q: queue.Queue = queue.Queue()
stop = threading.Event()
done = threading.Event()

returned_at = threading.Event()
t = threading.Thread(target=lambda: (sp.stream_tts_to_speaker(text_q, stop, done, provider="deepgram"),
                                     returned_at.set()), daemon=True)

# Driven on a worker thread so we can observe the done-event ordering.
for d in Deltas:
    text_q.put(d)
text_q.put(None)
t.start()
returned_at.wait(timeout=30)
ok_done = done.wait(timeout=30)

order = [c[0] if isinstance(c, tuple) else c for c in fake.calls]
feeds = [c[1] for c in fake.calls if isinstance(c, tuple)]

print("call order        :", order)
print("feeds             :", feeds)
print("frames written    :", device.frames, "(expected", len(Deltas) * 240, ")")
print("device started    :", device.started)
print("tts_done_event set:", ok_done)

assert order[0] == "open", order
assert order[-1] == "close", order
assert order[-2] == "flush", order
assert feeds == [d.strip() for d in Deltas], feeds
assert device.frames == len(Deltas) * 240, device.frames
assert ok_done and returned_at.is_set()
print("\nE2E PASS: open -> 4 feeds -> flush -> close, audio drained, done-event set after playback.")
