# Deepgram Flux streaming — core patches

## v2.1 (post-split layout, post-10k jump) — core-combined-v2-postsplit.patch — CURRENT
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
