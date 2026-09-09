# deepgram-voice

Deepgram voice plugins for Hermes Agent: Flux WebSocket streaming STT + streaming TTS.

This is the source-of-truth repo for two plugins that install into
`~/.hermes/plugins/`:

| Plugin dir in repo | Live install path |
|---|---|
| `plugins/tts-deepgram/` | `~/.hermes/plugins/tts/deepgram/` |
| `plugins/transcription-deepgram/` | `~/.hermes/plugins/transcription/deepgram/` |

The TTS plugin subclasses `tools.tts_streaming.StreamingTTSProvider` and sets
`supports_streaming = True` — LLM deltas are forwarded over the Flux WebSocket in
real time (`feed()` / `iter_audio()` protocol), no sentence chopping, no batch
file-upload. That incremental protocol requires small core patches (below).

`patches/` holds those core patches — they do NOT survive `hermes update` and must
be re-applied. See `patches/README-v2.md` for the restore procedure and what to do
if upstream moves the code again.

## Install / sync

```bash
cp -r plugins/tts-deepgram ~/.hermes/plugins/tts/deepgram
cp -r plugins/transcription-deepgram ~/.hermes/plugins/transcription/deepgram
```

Deepgram is selected via `config.yaml` / `hermes tools` CLI (desktop UI dropdown
gap is accepted — renderer sandbox can't reach the Python plugin registry).

## Credentials

`DEEPGRAM_API_KEY` in `~/.hermes/.env`.

## Reapply core patches after hermes update

```bash
cd ~/.hermes/hermes-agent   # Windows: %LOCALAPPDATA%/hermes/hermes-agent
git apply --check patches/core-combined-v2-postsplit.patch
git apply patches/core-combined-v2-postsplit.patch
```

If `--check` fails: upstream moved the code again — grep for the new homes and
hand-port (do NOT 3-way apply blindly; file splits produce cross-file conflict
blocks). Details in `patches/README-v2.md`.
