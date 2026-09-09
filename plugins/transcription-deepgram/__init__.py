"""Deepgram Flux Multilingual STT provider plugin.

Implements :class:`TranscriptionProvider` against Deepgram's Flux
conversational STT. Flux is **streaming-only** on ``/v2/listen``
(WebSocket) — there is no pre-recorded REST path for ``flux-*`` models.
This plugin opens a short-lived WebSocket, streams the audio file in
~80ms PCM chunks (or the raw container bytes), and returns the final
transcript assembled from ``TurnInfo`` events.

Default model: ``flux-general-multi`` (10 languages + optional
``language_hint``). English-only alternative: ``flux-general-en``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from agent.transcription_provider import TranscriptionProvider

logger = logging.getLogger(__name__)

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v2/listen"
DEFAULT_MODEL = "flux-general-multi"
# ~80ms of 16 kHz mono s16le = 16000 * 0.08 * 2 = 2560 bytes
_CHUNK_BYTES = 2560
_CONNECT_TIMEOUT = 15.0
_RECV_IDLE_TIMEOUT = 8.0  # seconds of silence after last audio before we finalize


def _api_key() -> str:
    from hermes_cli.config import get_env_value

    return (get_env_value("DEEPGRAM_API_KEY") or "").strip()


def _load_stt_cfg() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        stt = cfg.get("stt") if isinstance(cfg, dict) else None
        section = stt.get("deepgram") if isinstance(stt, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load stt.deepgram config: %s", exc)
        return {}


def _read_audio(path: Path) -> Tuple[bytes, Optional[int], Optional[str], bool]:
    """Return (payload, sample_rate, encoding, is_raw_pcm).

    Prefer decoding WAV to linear16 so we can advertise encoding/sample_rate
    to Flux. For other containers, stream the raw bytes and let Deepgram
    auto-detect (omit encoding/sample_rate per docs).
    """
    suffix = path.suffix.lower()
    if suffix == ".wav":
        try:
            with wave.open(str(path), "rb") as wf:
                channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                rate = wf.getframerate()
                nframes = wf.getnframes()
                frames = wf.readframes(nframes)
            # Downmix stereo → mono if needed (Discord path is already mono)
            if channels == 2 and sampwidth == 2:
                # Average L/R
                import array

                arr = array.array("h")
                arr.frombytes(frames)
                mono = array.array("h")
                for i in range(0, len(arr), 2):
                    mono.append(int((arr[i] + arr[i + 1]) / 2))
                frames = mono.tobytes()
                channels = 1
            if channels == 1 and sampwidth == 2:
                return frames, rate, "linear16", True
            # Unusual WAV — fall through to raw container send
        except wave.Error as exc:
            logger.debug("WAV parse failed (%s); sending container bytes", exc)

    data = path.read_bytes()
    return data, None, None, False


def _extract_transcript(msg: Dict[str, Any]) -> str:
    """Pull transcript text out of a Flux TurnInfo / channel message."""
    # Flux TurnInfo shape (v2): {"type":"TurnInfo", "transcript":"...", ...}
    if isinstance(msg.get("transcript"), str) and msg["transcript"].strip():
        return msg["transcript"].strip()

    # Defensive: some frames nest like v1 alternatives
    try:
        alts = msg["channel"]["alternatives"]
        if alts and isinstance(alts[0].get("transcript"), str):
            return alts[0]["transcript"].strip()
    except (KeyError, IndexError, TypeError):
        pass

    try:
        results = msg.get("results") or {}
        channels = results.get("channels") or []
        if channels:
            alts = channels[0].get("alternatives") or []
            if alts and isinstance(alts[0].get("transcript"), str):
                return alts[0]["transcript"].strip()
    except (KeyError, IndexError, TypeError):
        pass

    return ""


def _run_coro(coro):
    """Run *coro* even if a loop is already running (gateway threads)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # Nested loop — spin a private loop on this thread
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _transcribe_ws(
    *,
    api_key: str,
    audio: bytes,
    model: str,
    sample_rate: Optional[int],
    encoding: Optional[str],
    is_raw_pcm: bool,
    language_hint: Optional[List[str]] = None,
) -> str:
    import websockets
    from websockets.exceptions import ConnectionClosed

    params: Dict[str, Any] = {"model": model}
    if is_raw_pcm and encoding and sample_rate:
        params["encoding"] = encoding
        params["sample_rate"] = int(sample_rate)
    # language_hint only valid on flux-general-multi
    if language_hint and model == "flux-general-multi":
        # websockets urlencode doesn't do multi-value easily; join as repeated keys
        for lang in language_hint:
            # We'll add after base qs
            pass

    qs_parts = [urlencode(params)]
    if language_hint and model == "flux-general-multi":
        for lang in language_hint:
            qs_parts.append(urlencode({"language_hint": lang}))
    url = f"{DEEPGRAM_WS_URL}?{'&'.join(qs_parts)}"

    headers = {"Authorization": f"Token {api_key}"}
    pieces: List[str] = []
    final_pieces: List[str] = []

    async with websockets.connect(
        url,
        additional_headers=headers,
        open_timeout=_CONNECT_TIMEOUT,
        max_size=8 * 1024 * 1024,
        ping_interval=20,
    ) as ws:
        async def _sender() -> None:
            # Stream ~80ms chunks for raw PCM; larger chunks for containers
            step = _CHUNK_BYTES if is_raw_pcm else max(_CHUNK_BYTES * 4, 8192)
            for i in range(0, len(audio), step):
                await ws.send(audio[i : i + step])
                # Pace raw PCM roughly real-time-ish but faster (4x) so long
                # memos don't take wall-clock duration. Containers can go full-tilt.
                if is_raw_pcm:
                    await asyncio.sleep(0.02)
            # Close the audio stream so Flux flushes the final turn
            try:
                await ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:  # noqa: BLE001
                # Some SDK versions accept empty binary close; try both
                try:
                    await ws.send(b"")
                except Exception:  # noqa: BLE001
                    pass

        async def _receiver() -> None:
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=_RECV_IDLE_TIMEOUT)
                    if isinstance(raw, bytes):
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(msg, dict):
                        continue

                    mtype = str(msg.get("type") or msg.get("message_type") or "")
                    text = _extract_transcript(msg)
                    if text:
                        # Prefer finalized turn transcripts
                        if mtype in {"TurnInfo", "EndOfTurn", "Results", "final"}:
                            # Replace running hypothesis with final for this turn
                            if mtype == "EndOfTurn" or msg.get("event") == "EndOfTurn":
                                final_pieces.append(text)
                            else:
                                # Keep latest interim for this turn
                                if pieces and not final_pieces:
                                    pieces[-1] = text
                                else:
                                    pieces.append(text)
                        else:
                            pieces.append(text)

                    # Explicit end signals
                    if mtype in {"CloseStream", "Metadata"} and msg.get("duration") is not None:
                        # Metadata often arrives after CloseStream ack
                        if final_pieces or pieces:
                            break
                    if mtype == "Error":
                        err = msg.get("description") or msg.get("message") or str(msg)
                        raise RuntimeError(f"Deepgram Flux error: {err}")
            except asyncio.TimeoutError:
                # Idle after audio drained — treat as done
                return
            except ConnectionClosed:
                return

        sender = asyncio.create_task(_sender())
        receiver = asyncio.create_task(_receiver())
        try:
            await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_EXCEPTION,
            )
            # Let receiver drain remaining finals briefly
            if not receiver.done():
                try:
                    await asyncio.wait_for(receiver, timeout=3.0)
                except asyncio.TimeoutError:
                    receiver.cancel()
            if sender.done() and sender.exception():
                raise sender.exception()  # type: ignore[misc]
            if receiver.done() and receiver.exception():
                raise receiver.exception()  # type: ignore[misc]
        finally:
            for t in (sender, receiver):
                if not t.done():
                    t.cancel()

    # Prefer finalized turns; fall back to last hypotheses
    if final_pieces:
        return " ".join(final_pieces).strip()
    # Deduplicate consecutive identical interims
    out: List[str] = []
    for p in pieces:
        if not out or out[-1] != p:
            out.append(p)
    return " ".join(out).strip()


class DeepgramSTTProvider(TranscriptionProvider):
    """Deepgram Flux STT via ``wss://api.deepgram.com/v2/listen``."""

    @property
    def name(self) -> str:
        return "deepgram"

    @property
    def display_name(self) -> str:
        return "Deepgram Flux"

    def is_available(self) -> bool:
        if not _api_key():
            return False
        try:
            import websockets  # noqa: F401
        except ImportError:
            return False
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "flux-general-multi",
                "display": "Flux Multilingual (conversational)",
                "languages": ["en", "es", "fr", "de", "it", "pt", "nl", "ja", "ko", "zh"],
            },
            {
                "id": "flux-general-en",
                "display": "Flux English (conversational)",
                "languages": ["en"],
            },
        ]

    def default_model(self) -> Optional[str]:
        cfg = _load_stt_cfg()
        m = cfg.get("model") if isinstance(cfg.get("model"), str) else None
        return m or DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Deepgram Flux (STT)",
            "badge": "paid",
            "tag": "Flux Multilingual — streaming /v2/listen. Best quality for long voice memos.",
            "env_vars": [
                {
                    "key": "DEEPGRAM_API_KEY",
                    "prompt": "Deepgram API key",
                    "url": "https://console.deepgram.com/",
                },
            ],
        }

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        api_key = _api_key()
        if not api_key:
            return {
                "success": False,
                "transcript": "",
                "error": (
                    "DEEPGRAM_API_KEY is not set. Add it to ~/.hermes/.env "
                    "or set it in your shell, then /reset."
                ),
                "provider": self.name,
            }

        try:
            import websockets  # noqa: F401
        except ImportError:
            return {
                "success": False,
                "transcript": "",
                "error": (
                    "Python package 'websockets' is required for Deepgram Flux STT. "
                    "Install with: pip install websockets"
                ),
                "provider": self.name,
            }

        path = Path(file_path).expanduser()
        if not path.is_file():
            return {
                "success": False,
                "transcript": "",
                "error": f"Audio file not found: {file_path}",
                "provider": self.name,
            }

        cfg = _load_stt_cfg()
        chosen_model = (
            model
            or (cfg.get("model") if isinstance(cfg.get("model"), str) else None)
            or DEFAULT_MODEL
        )

        # language_hint for multi model
        hints: Optional[List[str]] = None
        lang = language or (
            cfg.get("language") if isinstance(cfg.get("language"), str) else None
        )
        if lang and chosen_model == "flux-general-multi":
            hints = [lang]
        cfg_hints = cfg.get("language_hint")
        if isinstance(cfg_hints, list) and cfg_hints:
            hints = [str(h) for h in cfg_hints]
        elif isinstance(cfg_hints, str) and cfg_hints.strip():
            hints = [cfg_hints.strip()]

        try:
            audio, sample_rate, encoding, is_raw = _read_audio(path)
        except OSError as exc:
            return {
                "success": False,
                "transcript": "",
                "error": f"Could not read audio file: {exc}",
                "provider": self.name,
            }

        if not audio:
            return {
                "success": False,
                "transcript": "",
                "error": f"Audio file is empty: {path.name}",
                "provider": self.name,
            }

        logger.info(
            "Transcribing %s with Deepgram Flux (%s, %d bytes, raw_pcm=%s)...",
            path.name,
            chosen_model,
            len(audio),
            is_raw,
        )

        try:
            transcript = _run_coro(
                _transcribe_ws(
                    api_key=api_key,
                    audio=audio,
                    model=chosen_model,
                    sample_rate=sample_rate,
                    encoding=encoding,
                    is_raw_pcm=is_raw,
                    language_hint=hints,
                )
            )
        except Exception as exc:  # noqa: BLE001 — convert to envelope
            logger.warning("Deepgram Flux STT failed: %s", exc, exc_info=True)
            return {
                "success": False,
                "transcript": "",
                "error": f"Deepgram Flux STT failed: {exc}",
                "provider": self.name,
            }

        if not transcript:
            return {
                "success": False,
                "transcript": "",
                "error": (
                    f"Deepgram Flux returned no transcript for {path.name}. "
                    "Empty audio, wrong format, or connection closed early."
                ),
                "provider": self.name,
            }

        return {
            "success": True,
            "transcript": transcript,
            "provider": self.name,
        }


def register(ctx) -> None:
    """Plugin entry point — Hermes calls this at discovery."""
    ctx.register_transcription_provider(DeepgramSTTProvider())
