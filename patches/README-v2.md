# Deepgram Flux streaming — core patches

## v2.2 (discord-voice-ptt-silence.patch) — Discord voice RX hardening — CURRENT
- Regenerated: 2026-09-27, against hermes-agent commit d0288be5b3. First content
  update to the discord patch (v1 was the 2026-09-08-era PTT + /voicemode +
  silence-duration work). Trigger: Discord voice captured 0.2s scraps across
  whole sessions (2026-09-27) after Discord's server-side DAVE E2EE rollout
  reached the guild. Two root causes, both fixed:
    1. DAVE session snapshot race — `VoiceReceiver.start()` snapshotted
       `conn.dave_session`, but discord.py creates/reinits it asynchronously
       (and can REPLACE the object mid-call). A snapshot taken before the MLS
       handshake lands is None → DAVE ciphertext decoded as plaintext → every
       packet dropped. Fix: `_current_dave_session()` reads the LIVE session
       per packet, guarded by `isinstance(x, davey.DaveSession)` (the guard
       keeps mock-based tests on the snapshot fallback).
    2. PTT/open-mic mode mismatch — config said PTT but the Captain talks
       open-mic; the receiver waited for op-5 releases that never came and the
       120s ghost-discard shredded the buffers (config fix:
       discord.voice_ptt_mode=false; code hardening: op-5 release frames are
       accepted without a prior user_id binding, rate-limited raw op-5 logging).
  Also: drop-reason counters (`_rx_stats`: ok/decrypt_fail/dave_fail/opus_err,
  INFO every 500) — decode-outcome only, control/keepalive packets excluded.
- Verified 2026-09-27: T1–T5 real-class PTT tests PASS; reverse-check CLEAN on
  the d0288be5b3 working tree; forward-apply CLEAN on vanilla HEAD (temp repo);
  live call confirmed by the Captain (open-mic, 110KB utterances transcribed).
- Supersedes the discord hunk inside v2.1's description above (that file needed
  no porting for the CORE patch; this patch is its own file).

## v2.1 (post-split layout, post-10k jump) — core-combined-v2-postsplit.patch
- Regenerated: 2026-09-26, against hermes-agent commit d0288be5b3 (main), after the
  2026-09-26 update (31d0a242 → d0288be5, 10,301 commits; 104 touched the five
  patched files). Same file set and hunk intent as v2 (2026-09-08, b2aa855b62);
  three files needed a hand-port because upstream rewrote the surroundings
  (xAI streaming rewrite) without absorbing the incremental API:
    tts_streaming.py   — merged upstream's provisional-sample_rate docstring
                         paragraph + @available() staticmethod with the two-modes
                         doc; supports_streaming field unchanged.
    tts_tool_speaker.py / audio.py — upstream's SentenceChunker.from_config(cfg)
                         (tts.streaming.min_len) used in the ported branches;
                         everything else ported verbatim.
  helpers.ts and discord/adapter.py needed NO porting (regions untouched
  upstream; both applied clean via git apply).
- Supersedes core-combined.patch (v1), which targeted the pre-split files
  (tools/tts_tool.py, hermes_cli/web_server.py) and NO LONGER APPLIES — upstream split:
    _visible_providers   → hermes_cli/tools_config_providers.py (hunk dropped; logic
                            already upstream via _PLUGIN_ROW_BUILDERS)
    speak_stream_ws      → hermes_cli/web_routers/audio.py
    stream_tts_to_speaker→ tools/tts_tool_speaker.py
    StreamingTTSProvider → tools/tts_streaming.py (unchanged path)
- Differences vs v1: single_shot branch dropped (Deepgram is the only consumer and uses
  the incremental protocol); producer thread joined before return so tts_done_event
  fires only after playback drains (prevents mic-reopening-over-own-voice).
- Contents: incremental protocol on StreamingTTSProvider (supports_streaming,
  open_session/feed/iter_audio/close_session) + incremental drain path in
  _StreamerPlayback + incremental branches in stream_tts_to_speaker and
  speak_stream_ws (+ forced plugin discovery in _resolve) + inert helpers.ts stub.

## Restore after hermes update
    cd ~/.hermes/hermes-agent   # Windows: %LOCALAPPDATA%/hermes/hermes-agent
    git apply --check patches/core-combined-v2-postsplit.patch   # from this dir
    git apply patches/core-combined-v2-postsplit.patch
If --check fails: upstream moved the code again. Do NOT 3-way apply blindly (file
splits produce cross-file conflict blocks). Locate the new homes:
    grep -rn "def speak_stream_ws\|def stream_tts_to_speaker\|class StreamingTTSProvider" hermes_cli/ tools/
…then hand-port. Verify: py_compile the touched files, then run the E2E checks
(DeepgramStreamer must resolve with supports_streaming=True).

## Verified 2026-09-26 (post v2.1 restore)
- Real plugin resolves: hermes_plugins.tts__deepgram.streaming.DeepgramStreamer,
  supports_streaming=True, incremental proto present.
- Device-free E2E: open → 4 feeds → flush → close, 960/960 frames, done-event set
  after playback.
- ptt_real_class_tests T1–T5 PASS against the real VoiceReceiver.
- Both patches reverse-check clean against d0288be5b3 working tree.
