import sys
sys.path.insert(0, r"C:/Users/tenta/AppData/Local/hermes/hermes-agent")
from hermes_cli.plugins import _ensure_plugins_discovered
_ensure_plugins_discovered()
from tools.tts_streaming import resolve_streaming_provider, _REGISTRY
print("registry:", sorted(_REGISTRY))
inst = resolve_streaming_provider({"provider": "deepgram"})
print("resolved:", (type(inst).__module__ + "." + type(inst).__name__) if inst else None)
print("supports_streaming:", getattr(inst, "supports_streaming", None))
print("has incremental proto:", all(hasattr(inst, m) for m in ("open_session","feed","iter_audio","close_session")))
