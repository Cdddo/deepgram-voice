"""Deepgram Flux TTS streaming provider (WebSocket /v2/speak).

Registers into Hermes' ``tools.tts_streaming`` registry with
``supports_streaming = True`` so the dispatcher drives us incrementally:

* ``open_session()`` — open the WebSocket, start a background reader task.
* ``feed(delta)`` — forward each LLM text delta as a Flux ``Speak`` frame
  (no Flush until turn end). Flux starts streaming audio the moment it has
  enough text, so time-to-first-audio is dominated by the LLM delta
  interval plus one WS round-trip — not by any Hermes-side idle delay.
* ``iter_audio()`` — drain any PCM chunks the reader has produced.
* ``close_session()`` — send ``Flush``, drain final audio, then ``Close``,
  join the reader, close the socket.

This is materially lower latency than the one-shot ``stream(text)`` path:
audio begins playing while the LLM is still generating.

Protocol (Flux EA, 2026-08):
  Client → ``{"type":"Speak","text":...}`` (many) → ``{"type":"Flush"}`` → ``{"type":"Close"}``
  Server → binary linear16 frames between ``SpeechStarted`` and
           ``SpeechMetadata``; ``Flushed`` / ``SessionMetadata`` close out.
  Audio may begin streaming BEFORE you call ``Flush``.

Sample rate locked to 24 kHz to match the other Hermes streamers
(ElevenLabs / OpenAI / xAI / Gemini).
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

DEEPGRAM_TTS_WS = "wss://api.deepgram.com/v2/speak"
DEFAULT_VOICE = "flux-colin-en"
SAMPLE_RATE = 24000
# Cap on the in-session audio queue. Audio chunks are 4 KiB typical;
# 256 chunks ≈ 1 MiB ≈ 11 s of buffered 24 kHz int16 mono.
_AUDIO_QUEUE_MAX = 256


def _api_key() -> str:
    try:
        from hermes_cli.config import get_env_value

        return (get_env_value("DEEPGRAM_API_KEY") or "").strip()
    except Exception:
        return ""


def _normalize_voice(voice: Optional[str]) -> str:
    if not voice or not str(voice).strip():
        return DEFAULT_VOICE
    v = str(voice).strip().lower()
    if v.startswith("flux-") or v.startswith("aura-"):
        return v
    return f"flux-{v}-en"


def _snap_speed(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    snapped = round(f / 0.05) * 0.05
    return max(0.85, min(1.15, round(snapped, 2)))


def _clamp_expressivity(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(max(-2, min(2, int(value))))
    except (TypeError, ValueError):
        return None


def register_streaming_provider() -> None:
    """Install Deepgram into ``tools.tts_streaming._REGISTRY``.

    Safe to call multiple times. Imports are deferred so a missing
    ``tools.tts_streaming`` (older Hermes) doesn't break plugin load —
    batch TTS still works without the streamer.
    """
    try:
        from tools.tts_streaming import StreamingTTSProvider, register
    except Exception as exc:  # noqa: BLE001
        logger.debug("tts_streaming unavailable; Deepgram streamer skipped: %s", exc)
        return

    @register("deepgram")
    class DeepgramStreamer(StreamingTTSProvider):
        """Flux TTS WebSocket → raw linear16 PCM at 24 kHz mono int16.

        Incremental: dispatcher pumps deltas via ``feed()`` and drains
        audio via ``iter_audio()`` while the LLM is still generating.
        This is materially lower-latency than calling ``stream(text)``
        once per chunk.
        """

        sample_rate = SAMPLE_RATE
        supports_streaming = True

        def __init__(self, tts_config: Dict, section: Dict) -> None:
            super().__init__(tts_config, section)
            # Session state — set by open_session(), cleared by close_session().
            self._ws = None              # websockets.WebSocketClientProtocol
            self._loop: Optional[asyncio.AbstractEventLoop] = None
            self._loop_thread: Optional[threading.Thread] = None
            self._loop_ready: Optional[threading.Event] = None
            self._reader_task = None
            self._audio_q: "queue.Queue[bytes]" = queue.Queue(maxsize=_AUDIO_QUEUE_MAX)
            self._session_done = threading.Event()
            self._session_error: Optional[BaseException] = None
            self._flush_event = threading.Event()
            # Backwards-compat: stream() used by sync callers (the
            # text_to_speech tool's non-streaming path, for example).
            self._lock = threading.Lock()

        @staticmethod
        def available() -> bool:
            return bool(_api_key())

        # --- One-shot path (kept for backwards compatibility) ---------------

        def stream(self, text: str) -> "Iterator[bytes]":
            """One-shot stream: open a WS, send the whole text, drain.

            Slightly higher time-to-first-audio than the incremental path
            because we wait for the full text before sending ``Flush``,
            but still single round-trip and Flux chunks internally.
            Pure-async — does not share state with the incremental
            open_session()/feed()/iter_audio() path.
            """
            from tools.tts_streaming import _capped

            yield from _capped(
                iter(self._collect_async(text)),
                "Deepgram Flux streaming TTS (one-shot)",
            )

        def _collect_async(self, text: str) -> List[bytes]:
            return asyncio.run(self._oneshot_async(text))

        async def _oneshot_async(self, text: str) -> List[bytes]:
            """Dedicated async path for one-shot callers."""
            import websockets

            api_key = _api_key()
            if not api_key:
                raise RuntimeError("DEEPGRAM_API_KEY is not set")

            voice = _normalize_voice(
                self.section.get("voice")
                or self.section.get("voice_id")
                or self.section.get("model")
            )
            params: Dict[str, Any] = {
                "model": voice,
                "encoding": "linear16",
                "sample_rate": SAMPLE_RATE,
            }
            speed = _snap_speed(self.section.get("speed"))
            if speed is not None:
                params["speed"] = speed
            expr = _clamp_expressivity(self.section.get("expressivity"))
            if expr is not None:
                params["expressivity"] = expr

            qs = urlencode(params)
            ws_url = f"{DEEPGRAM_TTS_WS}?{qs}"
            frames: List[bytes] = []

            async def _run(extra: Optional[Dict[str, Any]] = None) -> None:
                kwargs = extra if extra is not None else {
                    "additional_headers": {"Authorization": f"Token {api_key}"},
                }
                async with websockets.connect(ws_url, **kwargs) as ws:
                    await ws.send(json.dumps({"type": "Speak", "text": text}))
                    await ws.send(json.dumps({"type": "Flush"}))
                    try:
                        async for message in ws:
                            if isinstance(message, (bytes, bytearray, memoryview)):
                                frames.append(bytes(message))
                                continue
                            try:
                                envelope = json.loads(message)
                            except (TypeError, ValueError):
                                continue
                            etype = envelope.get("type")
                            if etype in {"SpeechMetadata", "SessionMetadata"}:
                                if etype == "SpeechMetadata":
                                    try:
                                        await ws.send(json.dumps({"type": "Close"}))
                                    except Exception:
                                        pass
                                return
                            if etype == "Error":
                                raise RuntimeError(
                                    f"Deepgram Flux TTS error: "
                                    f"{envelope.get('description') or envelope.get('code') or envelope}"
                                )
                    except Exception as exc:
                        if exc.__class__.__name__ in {
                            "ConnectionClosed",
                            "ConnectionClosedOK",
                        }:
                            return
                        raise

            try:
                await _run()
            except TypeError:
                await _run(extra={"extra_headers": {"Authorization": f"Token {api_key}"}})
            return frames

        # --- Incremental protocol ------------------------------------------

        def open_session(self) -> None:
            """Open the WebSocket session (sync wrapper)."""
            self.open_session_sync()

        def open_session_sync(self) -> None:
            """Spin up a dedicated event loop + reader thread + WebSocket."""
            with self._lock:
                if self._ws is not None:
                    return  # already open
                self._audio_q = queue.Queue(maxsize=_AUDIO_QUEUE_MAX)
                self._session_done.clear()
                self._session_error = None
                self._flush_event.clear()

                # Dedicated loop + thread for the WS reader. asyncio.run
                # would tear down the loop on return, so we drive it
                # manually from a worker thread that calls run_forever().
                self._loop = asyncio.new_event_loop()
                self._loop_ready = threading.Event()
                self._loop_thread = threading.Thread(
                    target=self._run_loop,
                    name="deepgram-flux-reader",
                    daemon=True,
                )
                self._loop_thread.start()
                self._loop_ready.wait(timeout=5.0)

                # Schedule the WS reader task on the loop.
                future = asyncio.run_coroutine_threadsafe(
                    self._reader_main(), self._loop
                )
                # Wait for the socket to open (or fail).
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    if self._ws is not None or self._session_error is not None:
                        break
                    threading.Event().wait(0.02)
                if self._session_error is not None:
                    raise self._session_error
                if self._ws is None:
                    # Cancel the task to avoid an orphan loop.
                    try:
                        future.cancel()
                    except Exception:
                        pass
                    raise RuntimeError("Deepgram Flux session did not open")
                self._reader_task = future

        def _run_loop(self) -> None:
            """Drive the dedicated event loop until close_session stops it."""
            asyncio.set_event_loop(self._loop)
            self._loop_ready.set()
            try:
                self._loop.run_forever()
            finally:
                # Drain pending tasks, close the loop.
                try:
                    pending = asyncio.all_tasks(self._loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        self._loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception:
                    pass
                try:
                    self._loop.close()
                except Exception:
                    pass

        def feed(self, text_delta: str) -> None:
            """Forward an LLM text delta to Flux as a ``Speak`` frame."""
            if not text_delta:
                return
            ws = self._ws
            loop = self._loop
            if ws is None or loop is None or loop.is_closed():
                raise RuntimeError("Deepgram Flux session is not open")
            payload = json.dumps({"type": "Speak", "text": text_delta})
            asyncio.run_coroutine_threadsafe(ws.send(payload), loop)

        def flush(self) -> None:
            """Tell Flux the turn is complete — server emits remaining audio."""
            ws = self._ws
            loop = self._loop
            if ws is None or loop is None or loop.is_closed():
                return
            payload = json.dumps({"type": "Flush"})
            asyncio.run_coroutine_threadsafe(ws.send(payload), loop)
            self._flush_event.set()

        def iter_audio(self, timeout: float = 0.0) -> "Iterator[bytes]":
            """Yield any PCM chunks currently buffered. Non-blocking by default."""
            if self._session_error is not None:
                # Surface the error to the dispatcher on first drain.
                raise self._session_error
            deadline = None
            if timeout > 0:
                deadline = threading.Event()  # no-op timer
            while True:
                try:
                    chunk = self._audio_q.get_nowait()
                except queue.Empty:
                    if self._session_done.is_set():
                        return
                    if timeout <= 0:
                        return
                    # Bounded wait if the caller asked for one.
                    try:
                        chunk = self._audio_q.get(timeout=timeout)
                    except queue.Empty:
                        return
                yield chunk

        def close_session(self) -> None:
            self.close_session_sync()

        def close_session_sync(self) -> None:
            """Flush, drain final audio, Close, join reader, close socket."""
            with self._lock:
                if self._ws is None and self._loop is None:
                    return
                # Best-effort Close frame. Reader task cleans up the socket.
                try:
                    if self._ws is not None and self._loop is not None and not self._loop.is_closed():
                        asyncio.run_coroutine_threadsafe(
                            self._ws.send(json.dumps({"type": "Close"})), self._loop
                        )
                except Exception:
                    pass
                # Wait briefly for any final audio to land in the queue.
                threading.Event().wait(0.3)
                # Stop the loop; reader task will exit on next iteration.
                if self._loop is not None and not self._loop.is_closed():
                    try:
                        self._loop.call_soon_threadsafe(self._loop.stop)
                    except Exception:
                        pass
                if self._loop_thread is not None and self._loop_thread.is_alive():
                    self._loop_thread.join(timeout=3.0)
                if self._reader_task is not None:
                    try:
                        self._reader_task.result(timeout=1.0)
                    except Exception:
                        pass
                self._ws = None
                self._loop = None
                self._loop_thread = None
                self._reader_task = None
                self._session_done.set()

        # --- Reader task (runs on the dedicated event loop) ----------------

        async def _reader_main(self) -> None:
            import websockets

            api_key = _api_key()
            if not api_key:
                self._session_error = RuntimeError("DEEPGRAM_API_KEY is not set")
                self._session_done.set()
                return

            voice = _normalize_voice(
                self.section.get("voice")
                or self.section.get("voice_id")
                or self.section.get("model")
            )
            params: Dict[str, Any] = {
                "model": voice,
                "encoding": "linear16",
                "sample_rate": SAMPLE_RATE,
            }
            speed = _snap_speed(self.section.get("speed"))
            if speed is not None:
                params["speed"] = speed
            expr = _clamp_expressivity(self.section.get("expressivity"))
            if expr is not None:
                params["expressivity"] = expr

            qs = urlencode(params)
            ws_url = f"{DEEPGRAM_TTS_WS}?{qs}"

            connect_kwargs: Dict[str, Any] = {
                "additional_headers": {"Authorization": f"Token {api_key}"},
            }

            async def _run(extra: Optional[Dict[str, Any]] = None) -> None:
                kwargs = extra if extra is not None else connect_kwargs
                async with websockets.connect(ws_url, **kwargs) as ws:
                    # Publish the socket immediately — open_session_sync is
                    # polling self._ws. SpeechStarted only arrives AFTER
                    # the first Speak, so we cannot wait for it here.
                    self._ws = ws
                    try:
                        async for message in ws:
                            if isinstance(message, (bytes, bytearray, memoryview)):
                                try:
                                    self._audio_q.put(bytes(message))
                                except queue.Full:
                                    # Slow consumer: drop oldest. Better than blocking.
                                    try:
                                        self._audio_q.get_nowait()
                                        self._audio_q.put(bytes(message))
                                    except Exception:
                                        pass
                                continue
                            try:
                                envelope = json.loads(message)
                            except (TypeError, ValueError):
                                continue
                            etype = envelope.get("type")
                            if etype in {"SpeechMetadata", "SessionMetadata"}:
                                # All audio for this turn has already arrived.
                                self._session_done.set()
                                return
                            if etype == "Error":
                                self._session_error = RuntimeError(
                                    f"Deepgram Flux TTS error: "
                                    f"{envelope.get('description') or envelope.get('code') or envelope}"
                                )
                                self._session_done.set()
                                return
                            if etype == "Warning":
                                logger.debug(
                                    "Deepgram Flux TTS warning: %s",
                                    envelope.get("description") or envelope.get("code"),
                                )
                    except Exception as exc:
                        if exc.__class__.__name__ in {
                            "ConnectionClosed",
                            "ConnectionClosedOK",
                        }:
                            return
                        if self._session_error is None:
                            self._session_error = exc
                        self._session_done.set()

            try:
                await _run()
            except TypeError:
                # Older websockets: try extra_headers.
                await _run(extra={"extra_headers": {"Authorization": f"Token {_api_key()}"}})
            except Exception as exc:
                if self._session_error is None:
                    self._session_error = exc
            finally:
                self._session_done.set()

    # Touch the class so linters don't think it's unused; registration already happened.
    _ = DeepgramStreamer
    logger.info(
        "Deepgram Flux streaming TTS registered (incremental, sample_rate=%d)",
        SAMPLE_RATE,
    )
