# Deepgram Flux streaming — core patches

## v2 (post-split layout) — core-combined-v2-postsplit.patch — CURRENT
- Generated: 2026-09-08, against hermes-agent commit b2aa855b62 (main, post Sep-2026
  god-file decomposition).
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
    cd C:/Users/tenta/AppData/Local/hermes/hermes-agent
    git apply --check patches/core-combined-v2-postsplit.patch   # from this dir
    git apply patches/core-combined-v2-postsplit.patch
If --check fails: upstream moved the code again. Do NOT 3-way apply blindly (file
splits produce cross-file conflict blocks). Locate the new homes:
    grep -rn "def speak_stream_ws\|def stream_tts_to_speaker\|class StreamingTTSProvider" hermes_cli/ tools/
…then hand-port. Verify: py_compile the touched files, then run the E2E checks
(DeepgramStreamer must resolve with supports_streaming=True).

## Verified 2026-09-08
- Real plugin resolves: hermes_plugins.tts__deepgram.streaming.DeepgramStreamer,
  supports_streaming=True.
- Device-free E2E: open → 4 feeds → flush → close, audio drained, done-event set.
- Upstream suites: 28/29 pass (test_hybrid_prefetch_fires_http_immediately fails on a
  zero-delta timing assert, unrelated to the incremental path).
- Desktop UI dropdown gap remains accepted (deepgram selected via config.yaml / CLI).
