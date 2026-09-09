"""Deepgram Flux TTS provider plugin.

Implements :class:`TTSProvider` against Deepgram's Flux TTS batch REST
endpoint (``POST /v2/speak``). Default voice: ``flux-colin-en`` (British
male, Adult). Voice is fully configurable via ``tts.deepgram.voice`` or
the dispatcher ``voice=`` kwarg.

Flux TTS model strings: ``flux-{voice}-en`` (launch catalog is English-only;
``flux-{voice}-multi`` is planned). Free through 2026-09-12.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from agent.tts_provider import DEFAULT_OUTPUT_FORMAT, TTSProvider

logger = logging.getLogger(__name__)

DEEPGRAM_TTS_URL = "https://api.deepgram.com/v2/speak"
DEFAULT_VOICE = "flux-colin-en"

# Curated Flux English launch catalog (scraped 2026-08-31).
# Format: flux-{voice}-en. Multilingual (flux-*-multi) arrives later.
_FLUX_VOICES: List[Dict[str, Any]] = [
    {"id": "flux-colin-en", "display": "Colin (en-GB) — male, adult (default)", "language": "en-GB", "gender": "male"},
    {"id": "flux-alexis-en", "display": "Alexis (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-haley-en", "display": "Haley (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-jack-en", "display": "Jack (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-kai-en", "display": "Kai (en)", "language": "en-US", "gender": "male"},
    {"id": "flux-marcus-en", "display": "Marcus (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-miles-en", "display": "Miles (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-paige-en", "display": "Paige (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-sienna-en", "display": "Sienna (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-tanner-en", "display": "Tanner (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-wes-en", "display": "Wes (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-bruce-en", "display": "Bruce (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-cliff-en", "display": "Cliff (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-cole-en", "display": "Cole (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-conor-en", "display": "Conor (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-drew-en", "display": "Drew (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-donovan-en", "display": "Donovan (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-rufus-en", "display": "Rufus (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-sean-en", "display": "Sean (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-wade-en", "display": "Wade (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-bree-en", "display": "Bree (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-brittany-en", "display": "Brittany (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-brooke-en", "display": "Brooke (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-elise-en", "display": "Elise (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-gemma-en", "display": "Gemma (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-hannah-en", "display": "Hannah (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-heather-en", "display": "Heather (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-kelsey-en", "display": "Kelsey (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-kit-en", "display": "Kit (en)", "language": "en-US", "gender": "neutral"},
    {"id": "flux-maeve-en", "display": "Maeve (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-marcelo-en", "display": "Marcelo (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-meena-en", "display": "Meena (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-meghan-en", "display": "Meghan (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-naveen-en", "display": "Naveen (en) — male", "language": "en-US", "gender": "male"},
    {"id": "flux-priya-en", "display": "Priya (en) — female", "language": "en-US", "gender": "female"},
    {"id": "flux-sharon-en", "display": "Sharon (en) — female", "language": "en-US", "gender": "female"},
]

# Batch REST encoding map. Flux batch supports compressed containers;
# streaming WebSocket is raw linear16 only (not used here).
_FORMAT_MAP = {
    "mp3": {"encoding": "mp3"},
    "wav": {"encoding": "linear16", "container": "wav"},
    "opus": {"encoding": "opus", "container": "ogg"},
    "ogg": {"encoding": "opus", "container": "ogg"},
    "flac": {"encoding": "flac"},
}


def _api_key() -> str:
    from hermes_cli.config import get_env_value

    return (get_env_value("DEEPGRAM_API_KEY") or "").strip()


def _load_tts_cfg() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        tts = cfg.get("tts") if isinstance(cfg, dict) else None
        section = tts.get("deepgram") if isinstance(tts, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load tts.deepgram config: %s", exc)
        return {}


def _normalize_voice(voice: Optional[str]) -> str:
    """Accept full model string or bare name; always return flux-{voice}-en."""
    if not voice or not str(voice).strip():
        return DEFAULT_VOICE
    v = str(voice).strip().lower()
    if v.startswith("flux-"):
        return v
    if v.startswith("aura-"):
        # User might still pass Aura IDs; keep them as-is for /v1 fallback later.
        return v
    # Bare name → flux-{name}-en
    return f"flux-{v}-en"


class DeepgramTTSProvider(TTSProvider):
    """Deepgram Flux TTS via ``POST /v2/speak``."""

    @property
    def name(self) -> str:
        return "deepgram"

    @property
    def display_name(self) -> str:
        return "Deepgram Flux TTS"

    def is_available(self) -> bool:
        return bool(_api_key())

    def list_voices(self) -> List[Dict[str, Any]]:
        return list(_FLUX_VOICES)

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "flux",
                "display": "Flux TTS (v2/speak)",
                "languages": ["en"],
                "max_text_length": 2000,
            },
        ]

    def default_voice(self) -> Optional[str]:
        cfg = _load_tts_cfg()
        configured = cfg.get("voice") if isinstance(cfg.get("voice"), str) else None
        return _normalize_voice(configured or DEFAULT_VOICE)

    def default_model(self) -> Optional[str]:
        return "flux"

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Deepgram Flux TTS",
            "badge": "free",
            "tag": "Flux TTS — default flux-colin-en (British male). Free through 2026-09-12.",
            "env_vars": [
                {
                    "key": "DEEPGRAM_API_KEY",
                    "prompt": "Deepgram API key (shared with STT)",
                    "url": "https://console.deepgram.com/",
                },
            ],
        }

    @property
    def voice_compatible(self) -> bool:
        return True

    def synthesize(
        self,
        text: str,
        output_path: str,
        *,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        format: str = DEFAULT_OUTPUT_FORMAT,
        **extra: Any,
    ) -> str:
        api_key = _api_key()
        if not api_key:
            raise RuntimeError(
                "DEEPGRAM_API_KEY is not set. Add it to ~/.hermes/.env "
                "(or set it in the shell), then /reset."
            )

        cfg = _load_tts_cfg()
        # Precedence: caller voice → config tts.deepgram.voice → default Colin
        chosen = voice or (
            cfg.get("voice") if isinstance(cfg.get("voice"), str) else None
        ) or DEFAULT_VOICE
        model_string = _normalize_voice(chosen)

        fmt_key = (format or DEFAULT_OUTPUT_FORMAT).lower().strip()
        fmt_cfg = _FORMAT_MAP.get(fmt_key, _FORMAT_MAP[DEFAULT_OUTPUT_FORMAT])

        params: Dict[str, Any] = {
            "model": model_string,
            "encoding": fmt_cfg["encoding"],
        }
        if "container" in fmt_cfg:
            params["container"] = fmt_cfg["container"]

        # Flux TTS params (per Deepgram Flux docs, 2026-08-31):
        #   speed: 0.85–1.15 in 0.05 steps (default 1.0). Caller kwarg wins,
        #     then tts.deepgram.speed. Includes 1.0 baseline when passed
        #     explicitly so downstream introspection can distinguish
        #     "not set" from "return to default".
        #   expressivity: integer -2..+2 (default 0). Caller kwarg wins,
        #     then tts.deepgram.expressivity.
        cfg_speed = cfg.get("speed")
        chosen_speed = (
            float(speed) if speed is not None
            else float(cfg_speed) if isinstance(cfg_speed, (int, float))
            else None
        )
        if chosen_speed is not None:
            # Snap to nearest 0.05 step, clamp to documented range. Flux
            # rejects out-of-range values with 400; snapping is kinder than
            # letting the request fail (e.g. user types 0.92 → 0.90).
            snapped = round(chosen_speed / 0.05) * 0.05
            params["speed"] = max(0.85, min(1.15, round(snapped, 2)))

        expr_kwarg = extra.get("expressivity")
        cfg_expr = cfg.get("expressivity")
        chosen_expr = (
            expr_kwarg if expr_kwarg is not None
            else cfg_expr if isinstance(cfg_expr, (int, float))
            else None
        )
        if chosen_expr is not None:
            params["expressivity"] = int(max(-2, min(2, int(chosen_expr))))

        try:
            response = requests.post(
                DEEPGRAM_TTS_URL,
                params=params,
                headers={
                    "Authorization": f"Token {api_key}",
                    "Content-Type": "application/json",
                },
                json={"text": text},
                timeout=60,
                stream=True,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            body = ""
            if exc.response is not None:
                try:
                    body = exc.response.text[:400]
                except Exception:  # noqa: BLE001
                    body = str(exc)
            # Helpful hint when someone still has an Aura model string
            hint = ""
            if status == 400 and "aura" in model_string:
                hint = " (Flux TTS is /v2/speak with flux-*-en models, not Aura /v1)"
            raise RuntimeError(
                f"Deepgram Flux TTS error ({status}): {body}{hint}"
            ) from exc
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise RuntimeError(f"Could not reach Deepgram Flux TTS: {exc}") from exc

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as fh:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    fh.write(chunk)

        if out.stat().st_size == 0:
            raise RuntimeError(
                f"Deepgram Flux TTS returned empty audio for voice={model_string}"
            )

        logger.info(
            "Deepgram Flux TTS: %d chars → %s (voice=%s, format=%s)",
            len(text),
            out,
            model_string,
            fmt_key,
        )
        return str(out)


def register(ctx) -> None:
    """Plugin entry point — Hermes calls this at discovery."""
    ctx.register_tts_provider(DeepgramTTSProvider())
    # Also install the chunked-PCM streamer into tools.tts_streaming so
    # desktop/CLI/TUI sentence pipelines get real WebSocket streaming
    # instead of per-sentence batch. Failures are non-fatal — batch TTS
    # still works without the streamer.
    try:
        from .streaming import register_streaming_provider

        register_streaming_provider()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Deepgram streaming registration skipped: %s", exc)
