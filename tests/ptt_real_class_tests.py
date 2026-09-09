"""Real-class tests for VoiceReceiver PTT semantics (approved shape).

Approved design:
1. Eager SSRC bind (both modes) on first packet, inside the lock, one-shot.
2. PTT: eager bind synthesizes KEY-HELD only (missed press).
3. NO synth-release: flush only on real op-5 release + 0.25s grace
   (anchor = max(release, last packet)), or channel leave.
4. 120s stale discard stays as ghost-buffer leak prevention.
5. Grace-anchor fix: max(release, last_packet).

Drives the REAL VoiceReceiver from plugins/platforms/discord/adapter.py.
Packet flow is injected via _on_packet-compatible decryption bypass: we call
the internal buffer-extension path directly is NOT enough (must exercise the
eager-bind branch), so we stub decrypt + DAVE and call _on_packet with valid
RTP frames.
"""
import struct
import sys
import threading
import time

sys.path.insert(0, r"C:/Users/tenta/AppData/Local/hermes/hermes-agent")

from plugins.platforms.discord.adapter import VoiceReceiver


def make_rtp(ssrc, payload=b"\x00" * 64):
    header = struct.pack(">BBHII", 0x80, 0x78, 1, 0, ssrc)
    return header + b"\x00\x00\x00\x00" + payload  # header + 4-byte nonce


def make_receiver(ptt=True, secret=b"k" * 32, dave=False):
    r = VoiceReceiver.__new__(VoiceReceiver)
    # minimal init mirroring __init__ (avoids voice-client dependency)
    r._vc = None
    r._allowed_user_ids = {"42"}
    r._running = True  # _on_packet bails immediately otherwise
    r._secret_key = secret
    r._dave_session = None
    r._bot_ssrc = 999
    r._ssrc_to_user = {}
    r._lock = threading.Lock()
    import collections
    r._buffers = collections.defaultdict(bytearray)  # real __init__ uses defaultdict
    r._last_packet_time = {}
    r._decoders = {}
    r._paused = False
    r._packet_debug_count = 0
    r.SILENCE_THRESHOLD = 3.5
    r.MIN_SPEECH_DURATION = 0.5
    r.SILENCE_RMS = 0
    r.PTT_TAIL_GRACE = 0.25
    r.PTT_STALE_SECONDS = 120.0
    r._ptt_mode = ptt
    r._speaking_active = {}
    r._released_at = {}

    # stub the decode + NaCl path: _on_packet calls nacl.secret.Aead.decrypt
    # via import — patch _secret_key decrypt by replacing _on_packet's decrypt
    # dependency: easier to monkeypatch nacl import? No — simplest: craft the
    # fake RTP so decrypt succeeds via a real nacl box whose key we control.
    return r


def wire_real_decrypt(r):
    """Use a REAL nacl box so _on_packet's decrypt path works unmodified."""
    import nacl.secret
    r._secret_key = b"k" * 32
    return nacl.secret.Aead(r._secret_key)


def encrypt_packet(box, ssrc, payload=b"\x00" * 64):
    header = struct.pack(">BBHII", 0x80, 0x78, 1, 0, ssrc)
    # Receiver derives its nonce as 20 zeros + last-4-bytes-of-payload (all
    # zeros here). Encrypt with that SAME explicit nonce and strip the
    # EncryptedMessage's nonce prefix so the wire format matches:
    # header || ciphertext+tag || 4-byte nonce fragment.
    nonce = bytes(24)
    enc = box.encrypt(bytes(payload), bytes(header), nonce)
    ct = bytes(enc[24:])  # strip nacl's nonce prefix
    return header + ct + b"\x00\x00\x00\x00"


def feed_real(r, box, ssrc, n=1, payload=None):
    if payload is None:
        payload = b"\x00" * 64
    for _ in range(n):
        r._on_packet(encrypt_packet(box, ssrc, payload))


# Patch _infer_user_for_ssrc to avoid voice-client: bind to user 42 (sole allowed member).
def bind_inference(r, uid=42):
    def infer(ssrc):
        r._ssrc_to_user[ssrc] = uid
        return uid
    r._infer_user_for_ssrc = infer
    return infer


print("=== T1: PTT — press missed, release seen → flush on real release ===")
r = make_receiver(ptt=True)
box = wire_real_decrypt(r)
bind_inference(r)
feed_real(r, box, ssrc=100, n=20)          # audio flows, press event lost
assert r._ssrc_to_user.get(100) == 42, "eager bind failed"
assert r._speaking_active.get(100) is True, "key-held not synthesized"
time.sleep(0.1)
assert r.check_silence() == [], "no flush before release"
r.on_speaking_event(100, False)             # REAL release event arrives
time.sleep(0.3)                             # > grace
out = r.check_silence()
assert len(out) == 1 and out[0][0] == 42, f"T1 flush failed: {out}"
assert r._buffers.get(100, bytearray()) == bytearray()
print("T1 PASS")

print("=== T2: PTT — held-key 3s pause mid-utterance → NO flush (no pause limit) ===")
r = make_receiver(ptt=True)
box = wire_real_decrypt(r)
bind_inference(r)
feed_real(r, box, ssrc=200, n=10)
time.sleep(3.0)                             # pause > silence_duration 3.5? no, 3.0 < 3.5; but > old synth 2.0
out = r.check_silence()
assert out == [], f"pause flushed — synth-release still present or misfired: {out}"
feed_real(r, box, ssrc=200, n=5)            # speech resumes, same utterance
r.on_speaking_event(200, False)             # now release
time.sleep(0.3)
out = r.check_silence()
assert len(out) == 1, f"T2: full utterance not delivered: {out}"
print("T2 PASS")

print("=== T3: PTT — both events missed → buffer waits (not discarded at 120s in test window) ===")
r = make_receiver(ptt=True)
box = wire_real_decrypt(r)
bind_inference(r)
feed_real(r, box, ssrc=300, n=10)
time.sleep(0.5)
out = r.check_silence()
assert out == [], "flushed without any release"
assert r._buffers.get(300), "buffer lost before stale window"
# simulate the stale window passing: shrink it for test speed
r.PTT_STALE_SECONDS = 0.4
time.sleep(0.5)
out = r.check_silence()
assert out == [] and not r._buffers.get(300), "ghost buffer not cleaned"
print("T3 PASS")

print("=== T4: open-mic — eager bind fixes anonymous-drop (60s delay issue) ===")
r = make_receiver(ptt=False)
box = wire_real_decrypt(r)
bind_inference(r)
feed_real(r, box, ssrc=400, n=1600, payload=b"\x00" * 64)  # 102400B valid opus frames > 0.5s
assert r._ssrc_to_user.get(400) == 42, "open-mic eager bind failed"
time.sleep(3.6)                             # > silence_duration
out = r.check_silence()
assert len(out) == 1 and out[0][0] == 42, f"open-mic delivery failed: {out}"
print("T4 PASS")

print("=== T5: one-shot inference — no members walk per packet ===")
r = make_receiver(ptt=True)
box = wire_real_decrypt(r)
calls = {"n": 0}
def counting_infer(ssrc):
    calls["n"] += 1
    r._ssrc_to_user[ssrc] = 42
    return 42
r._infer_user_for_ssrc = counting_infer
feed_real(r, box, ssrc=500, n=25)
assert calls["n"] == 1, f"inference ran {calls['n']} times, expected 1 (per-packet walk)"
print("T5 PASS")

print("ALL REAL-CLASS PTT TESTS PASSED")
