# Persistent STT WebSocket — Refactor Plan

**Date:** 2026-09-11
**Status:** PLAN — not implemented. Review in a dedicated session.
**Owner:** Captain (design) / Atlas (implementation)
**Scope:** Deepgram Flux STT in the Discord voice path (`plugins/transcription/deepgram` + `plugins/platforms/discord/adapter.py`)

---

## 1. The one-line thesis

The STT WebSocket should stay **open for the whole voice call** — independent of push-to-talk. Today it is opened and closed **once per utterance**, and that is the root cause of the intermittent message loss.

---

## 2. Observed symptoms

Voice input in Discord intermittently drops whole utterances that were captured correctly on our side.

Evidence from the gateway/agent logs (Sept 9–10, 2026):

| When | What happened |
|---|---|
| 2026-09-09 02:00:17 / 02:01:23 / 02:02:26 | Three consecutive transcriptions started; each took ~65 s and returned **no text**. No error logged at the time (silent drop — the adapter returned early without logging the failure envelope). |
| 2026-09-09 02:34:40 | `Transcribing vc_listen_omoekiig.wav` (500 KB) → `Voice STT failed … 'Deepgram Flux returned no transcript'` after 13 s. |
| 2026-09-10 15:56, 20:00 | Same `no transcript` failure. |
| 2026-09-10 20:13–20:15 | **Three** `no transcript` failures in ~80 seconds. |
| All sessions | **Zero** `PTT release` log lines, ever. |

Two things are notable:

1. In every case **the audio arrived and was sent** — capture is not the loss point. The transcript is.
2. The failure is **silent from the user's perspective**: before 2026-09-09 the adapter returned early on an unsuccessful result without logging it at all. That logging was added (good), but the underlying failure remains.

---

## 3. Root cause

Both failure modes trace to one architectural choice: **one WebSocket connection per utterance (batch), instead of one per call (streaming).**

### Current path

```
Discord voice packet
  → VoiceReceiver buffers PCM per SSRC
  → check_silence() decides the utterance ended
  → _process_voice_input(): pcm_to_wav() → transcribe_audio()
  → DeepgramSTTProvider.transcribe()
  → _transcribe_ws(): websockets.connect()   ← NEW WS PER UTTERANCE
        send whole buffer → wait for finals → close
```

Relevant constants in `plugins/transcription/deepgram/__init__.py`:

- `_CONNECT_TIMEOUT = 15.0`
- `_RECV_IDLE_TIMEOUT = 8.0` — "seconds of silence after last audio before we finalize"

That 8 s idle timeout is a **workaround for a connection that should never have been ephemeral**. It exists solely to guess when to stop waiting on a short-lived socket.

### Failure mode A — the idle-timeout race ("no transcript")

Each utterance is a **cold** connection: TLS handshake, audio upload, then a race to receive the final transcript(s) before the 8 s idle window closes. When the finals don't land in time, the receiver returns whatever it has — often nothing — and the provider converts that into `Deepgram Flux returned no transcript`.

This is a race, which is exactly why the symptom is intermittent and correlates with longer utterances.

### Failure mode B — PTT waits for an event Discord never sends

In PTT mode the receiver closes a buffer **only** on Discord's `SPEAKING` release (`speaking=False`), with a small `PTT_TAIL_GRACE` (0.25 s) after it. There are **no synthetic releases in PTT mode** by design.

The log contains **zero** release events. With no release, a buffer is never flushed by the normal path; it survives only until `PTT_STALE_SECONDS` (120 s) and is then discarded as stale.

So in PTT mode, a single missing event costs an entire utterance — and there is no fallback to the silence timer.

> Note: the receiver reads `voice_ptt_mode` **at receiver start**, not per utterance. A config change while a receiver is running does not take effect until the bot rejoins the voice channel — so the live process and the config file drift apart easily.

### 3.1 Addendum — `voice_ptt_mode` is never actually read (config path mismatch)

Discovered 2026-09-12 while testing the Nova-3 batch path. **Relevant to the Flux path only** — the batch/Nova-3 path goes through the normal provider route that every other STT provider uses, so PTT segmentation does not apply there at all.

The two voice-input settings live at `discord.voice_*`:

```yaml
discord:
  voice_silence_duration: 3.5
  voice_ptt_mode: true
  extra:
    allow_admin_from: '<id>'
```

…but the receiver resolves them through `_config_value()`, which reads a different location:

```python
def _config_value(self, key, default, *, env_key=None):
    extra = self.config.extra if isinstance(getattr(self.config, "extra", None), dict) else {}
    value = extra.get(key)          # ← discord.extra.*, not discord.*
```

`discord.extra` contains only `allow_admin_from`, so:

- `voice_ptt_mode` → **always falls back to its default, `False`** — the receiver runs in open-mic mode no matter what the config says.
- `voice_silence_duration` → same problem; the configured value never applies.

Observed consequences (consistent with the logs):

- **Zero** `PTT release` events, ever — `on_speaking_event()` returns immediately on `if not self._ptt_mode: return`.
- Utterances still flush normally, because open-mic flushes on the silence timer. This masks the bug: everything appears to work, but PTT is a no-op.
- Toggling push-to-talk in the Discord client has no effect on Hermes's behaviour.

**Second layer of the same inconsistency:** the `/voicemode` handler reads and writes the *correct* location — `cfg["discord"]["voice_ptt_mode"]` on read, `save_config_value("discord.voice_ptt_mode", ...)` on write. So the command and the receiver do not agree on where the setting lives; even a working `/voicemode` would persist a value the receiver never reads.

**Fix options (decide when the Flux path is reworked):**

1. Point the receiver's read at `discord.<key>` (align it with the `/voicemode` handler and the config layout), or
2. Move the two keys under `discord.extra:` to match `_config_value()`.

Option 1 is preferred — the sibling setting `voice_channel_inactivity_timeout_seconds` is already read from `discord.<key>` and that path works, so `discord.<key>` is the consistent home.

**Note:** with the persistent-WebSocket design (§4), PTT stops being a flush mechanism and becomes a segmentation hint — so this bug matters less if the refactor lands. But the config read should still be fixed, or `voice_silence_duration` remains dead in every mode.

---

## 4. Target design

Split the two concerns that are currently entangled.

### Layer 1 — Transport (lifecycle: one per call)

A single Deepgram Flux WebSocket per voice session:

- opens when the bot joins the voice channel
- stays open for the duration of the call
- closes when the bot leaves
- reconnects transparently if it drops mid-call

**This layer is completely independent of PTT.**

### Layer 2 — Segmentation (policy: when to emit a segment)

The connection is continuous; the question becomes *when do we cut a segment and hand it to the agent*.

| Mode | Segmentation trigger |
|---|---|
| Push-to-talk | Discord `SPEAKING` release (`speaking=False`) |
| Open mic | Deepgram's `EndOfTurn` event |

PTT stops being a survival mechanism and becomes what it should have been all along: a button that says "cut here".

---

## 5. Why this fixes it

- **Eliminates the idle-timeout race.** A warm, persistent connection has no 8 s window to beat; finals arrive whenever they arrive. Failure mode A disappears.
- **Removes the dependency on Discord's release event for correctness.** Correctness now comes from Deepgram's own turn detection; PTT release is only ever a hint. Failure mode B disappears.
- **Kills the 120 s stale-buffer discard** — buffers no longer need to survive a missing event to be useful.
- **Unlocks interims.** A live stream gives partial transcripts, which opens the door to live captions or barge-in later. The batch path can never do this.
- **Simplifies the receiver.** No more silence heuristic tuning (`MIN_SPEECH_DURATION`, `SILENCE_THRESHOLD`) deciding the fate of an utterance. Those become safety nets, not the primary mechanism.

This is also what the original integration decision intended: *"Flux WebSocket streaming STT is the whole point"*. The design was right; the implementation drifted to batch.

---

## 6. Implementation outline

Not a one-line fix. Expect a receiver refactor plus a plugin-side streaming API.

1. **Plugin: add a streaming STT session API.**
   Mirror the TTS side's protocol shape (`StreamingTTSProvider`: `open_session` / `feed` / `iter_audio` / `close_session`) — but for STT: open a Flux socket, `feed(pcm)` continuously, `iter_events()` yielding interim + final + `EndOfTurn`.
   The TTS plugin already proves this pattern works against Deepgram Flux; reuse the structure.

2. **Receiver: feed frames instead of buffering to a file.**
   `VoiceReceiver` currently accumulates PCM per SSRC and hands a closed buffer to `_process_voice_input`. Instead, forward frames to the live session as they arrive. Keep a rolling buffer only for the current segment (so PTT/open-mic can still decide where to cut).

3. **Segmentation on top of the stream.**
   Emit a segment on `EndOfTurn` (open mic) or on the PTT release. Keep `voice_silence_duration` as a **fallback** for when neither arrives, plus a hard max-utterance cap as a safety net.

4. **Reconnect logic.**
   If the socket drops mid-call, reconnect and resume. Decide what to do with a segment in flight during a drop (emit it, or discard it loudly?).

5. **Backpressure.**
   If Deepgram is slower than realtime, don't grow an unbounded queue. Decide the drop policy and log it.

6. **Check whether core needs a hook.**
   The TTS side required core patches to register a streaming provider and to reach the incremental branch. STT may need the analogous seam (a streaming STT provider ABC + registry). Establish this **before** writing the plugin code — it determines whether this is a plugin-only change or needs a core patch family alongside `patches/core-combined-v2-postsplit.patch`.

7. **Keep or retire the batch path.**
   Options: keep `transcribe_audio(file)` as the fallback for non-voice surfaces (uploaded audio files, other platforms), and use the streaming session only for the Discord voice path. That is likely the cleanest split.

8. **Tests.**
   Follow the project rules: behaviour contracts, not change-detectors; never source-reading tests. Cover (a) a segment emitted on `EndOfTurn`, (b) a segment emitted on PTT release, (c) fallback segmentation when no turn event arrives, (d) reconnect mid-call.

---

## 7. Open questions

- Does Flux emit `EndOfTurn` reliably on every utterance, or can turns be missed? Verify against the real API before committing to it as the primary trigger.
- What is the right behaviour when the socket drops **mid-utterance**?
- Do we need barge-in / interim transcripts as a product feature, or is that scope creep? (Unlocked by this design, not required by it.)
- Is there an upstream-friendly shape for a streaming STT provider (analogous to `StreamingTTSProvider`)? If yes, this is also a candidate for an upstream PR rather than a fork-only patch.
- How does this interact with Discord's `DAVE` decryption path when SSRC is not yet known? (The receiver currently has an "unknown SSRC" branch.)

---

## 10. OPTION B — Traditional batch pipeline as Flux alternative (added 2026-09-12)

**Status:** implemented 2026-09-12 (plugin-side switch). NOT a replacement for this plan — a parallel escape hatch while the per-utterance WS bugs make calls unusable. Flux stays the primary path.

**Thesis (Captain, 2026-09-12):** Flux's low latency is worth little if the bugs won't let us talk. The plugin should allow **switching Discord voice STT from Flux to nova-3 (or any provider) via the traditional pipeline every other provider uses**: receiver buffers the utterance → WAV → `transcribe_audio()` → configured provider. That makes voice calls work with nova-3 REST (paid, high quality) OR Parakeet local (`:5092`, free) — STT gratis y funcional.

**Design sketch:**

1. New config key: `stt.deepgram.discord_mode: flux | batch` (default `flux` = today's behavior).
2. `discord_mode: batch` short-circuits the special path: `DeepgramSTTProvider.transcribe()` routes the receiver's WAV through `_transcribe_rest()` (nova-3, `language=multi`) instead of `_transcribe_ws()` — the REST route already exists and is proven (Telegram testbench, 2026-09-12).
3. Because `transcribe_audio()` is provider-agnostic, setting `stt.provider` to Parakeet (local command provider) with `discord_mode: batch` gives free local voice calls with zero Deepgram involvement.
4. The receiver/PTT/silence machinery is untouched — same utterance segmentation, different transcription backend. `hermes update`-safety: plugin-only change, no core edits.

**Tradeoff:** +1–2 s perceived latency per utterance (transcription starts after the pause/PTT release instead of streaming). Accepted — a working call with delay beats a broken call with speed.

**Relationship to this plan:** if batch mode proves reliable, the persistent-WS refactor becomes optional polish rather than urgent work. If Flux's bugs get fixed upstream first, batch mode remains as a zero-cost fallback. Either way the two coexist behind one config key.

---

## 8. References

**Live files (install tree: `~/.hermes/hermes-agent`)**
- `plugins/platforms/discord/adapter.py` — `VoiceReceiver` (buffer/silence/PTT bookkeeping), `_voice_listen_loop`, `_process_voice_input`, `_register_voicemode_slash`
  - `PTT_TAIL_GRACE`, `PTT_STALE_SECONDS`, `MIN_SPEECH_DURATION`, `set_ptt_mode()`
  - receiver instantiation reads `voice_ptt_mode` / `voice_silence_duration` **at start time**
- `plugins/transcription/deepgram/__init__.py` — `DeepgramSTTProvider`, `_transcribe_ws`, `_CONNECT_TIMEOUT`, `_RECV_IDLE_TIMEOUT`

**Plugin source of truth**
- `E:\Projects\Hermes Plugins\deepgram-voice\` — `plugins/`, `patches/`, `tests/`
  - `patches/core-combined-v2-postsplit.patch`, `patches/discord-voice-ptt-silence.patch`, `patches/README-v2.md`
- `~/.hermes/notes/deepgram-stt-plugin-plan.md` — the original STT/TTS plugin implementation plan (2026-08-31)

**Config (as of 2026-09-11)**
```yaml
discord:
  voice_silence_duration: 3.5
  voice_ptt_mode: true
```

**Session history**
- `20260831_175254_c0ae05` / `20260831_222634_5bc456` — Deepgram plugin build, streaming TTS, the integration decision to keep the plugin route
- `20260619_040015_b7006c95` — original Discord voice debugging notes (first-message drop, SSRC mapping, speaking-hook race)

---

## 9. Related but separate

- **`/voicemode` creates a thread** — a dispatch bug, unrelated to this plan. `template=None` entries in `_NATIVE_SLASH_COMMANDS` were all routed to the thread handler, so `voicemode` never installed its real handler. Fixed in commit `f266904b0e` (pushed to `Cdddo/hermes-agent`), but **`hermes update` reverted it in the install tree** — it needs re-applying. Whatever ships from this plan should be snapshotted as a patch in `deepgram-voice/patches/` so it survives updates.
- **`hermes update` reverts install-tree changes.** Anything landing from this plan must be snapshotted + reverse-checked in `patches/`, per the restore procedure in `patches/README-v2.md`.
