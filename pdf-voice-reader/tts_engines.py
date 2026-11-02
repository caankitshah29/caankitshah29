"""Text-to-speech back ends.

* EdgeVoice  - Microsoft Neural voices (needs internet). Default voice is
               "en-IN-NeerjaNeural": natural Indian-English female voice.
               Gives exact word timings, so the highlighter is precise.
* SapiVoice  - Voices installed in Windows (works offline), e.g.
               "Microsoft Heera - English (India)".

Every engine implements  synthesize(text, speed, out_path) -> Synth.
"""
from __future__ import annotations

import asyncio
import math
import os
import ssl
import sys
import threading
import wave
from dataclasses import dataclass, field


@dataclass
class Synth:
    path: str | None          # audio file, None when there was nothing to say
    duration_ms: float
    boundaries: list = field(default_factory=list)  # [(offset_ms, word)]; empty = estimate


class TTSError(Exception):
    pass


# --------------------------------------------------------------------- Edge
EDGE_VOICES = [
    ("Neerja - Indian English, Female (Neural, online)", "en-IN-NeerjaNeural"),
    ("Neerja Expressive - Indian English, Female (Neural, online)", "en-IN-NeerjaExpressiveNeural"),
    ("Prabhat - Indian English, Male (Neural, online)", "en-IN-PrabhatNeural"),
]

_ssl_configured = False


def _configure_edge_ssl():
    """Use the Windows certificate store when possible (works behind office
    proxies / antivirus that inspect HTTPS)."""
    global _ssl_configured
    if _ssl_configured:
        return
    _ssl_configured = True
    try:
        import edge_tts.communicate as cm
    except Exception:
        return
    if not hasattr(cm, "_SSL_CTX"):
        return
    ca_file = os.environ.get("PDF_READER_CA_FILE")
    try:
        if ca_file:
            cm._SSL_CTX = ssl.create_default_context(cafile=ca_file)
        elif sys.platform == "win32":
            import truststore  # optional
            cm._SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass


class EdgeVoice:
    kind = "online"
    ext = ".mp3"
    BYTES_PER_MS = 48000 / 8 / 1000  # edge-tts streams 24 kHz 48 kbit/s mono MP3

    def __init__(self, label: str, voice_id: str):
        self.label = label
        self.voice_id = voice_id
        self.key = "edge:" + voice_id

    def synthesize(self, text: str, speed: float, out_path: str) -> Synth:
        import edge_tts
        _configure_edge_ssl()
        pct = int(round((speed - 1.0) * 100))
        rate = f"{pct:+d}%"

        async def run():
            kwargs = dict(rate=rate)
            try:
                comm = edge_tts.Communicate(text, self.voice_id, boundary="WordBoundary", **kwargs)
            except TypeError:  # older edge-tts: word boundaries are the default
                comm = edge_tts.Communicate(text, self.voice_id, **kwargs)
            audio = bytearray()
            marks = []
            async for item in comm.stream():
                if item["type"] == "audio":
                    audio.extend(item["data"])
                elif item["type"] == "WordBoundary":
                    marks.append((item["offset"] / 10000.0, item["text"]))
            return bytes(audio), marks

        last_err = None
        for _attempt in range(3):
            try:
                audio, marks = asyncio.run(asyncio.wait_for(run(), timeout=45))
                break
            except Exception as e:  # network hiccup -> retry
                if type(e).__name__ == "NoAudioReceived":
                    return Synth(None, 0.0)
                last_err = e
        else:
            raise TTSError(
                "Could not reach the online Neural voice service.\n"
                "Check your internet connection, or choose an offline (Windows) voice.\n\n"
                f"Details: {last_err}"
            )
        if not audio:
            return Synth(None, 0.0)
        with open(out_path, "wb") as f:
            f.write(audio)
        duration = len(audio) / self.BYTES_PER_MS
        return Synth(out_path, duration, marks)


# --------------------------------------------------------------------- SAPI
def _sapi_dispatch():
    import pythoncom  # noqa: F401  (pywin32)
    import win32com.client
    return win32com.client


class SapiVoice:
    kind = "offline"
    ext = ".wav"

    def __init__(self, label: str, token_id: str):
        self.label = label
        self.token_id = token_id
        self.key = "sapi:" + token_id
        self._local = threading.local()  # COM objects must stay on their own thread

    def _get_voice(self):
        v = getattr(self._local, "voice", None)
        if v is None:
            import pythoncom
            pythoncom.CoInitialize()
            client = _sapi_dispatch()
            v = client.Dispatch("SAPI.SpVoice")
            token = _find_token(client, self.token_id)
            if token is not None:
                v.Voice = token
            self._local.voice = v
        return v

    def synthesize(self, text: str, speed: float, out_path: str) -> Synth:
        if not text.strip():
            return Synth(None, 0.0)
        try:
            client = _sapi_dispatch()
            v = self._get_voice()
            # SAPI rate is -10..10, where +10 is roughly 3x and -10 roughly 1/3x.
            v.Rate = max(-10, min(10, int(round(10 * math.log(speed) / math.log(3)))))
            stream = client.Dispatch("SAPI.SpFileStream")
            stream.Open(out_path, 3, False)  # 3 = SSFMCreateForWrite
            v.AudioOutputStream = stream
            v.Speak(text, 0)
            stream.Close()
            v.AudioOutputStream = None
        except Exception as e:
            raise TTSError(f"Windows voice failed: {e}")
        with wave.open(out_path, "rb") as w:
            duration = w.getnframes() * 1000.0 / max(w.getframerate(), 1)
        if duration < 1:
            return Synth(None, 0.0)
        return Synth(out_path, duration, [])


_SAPI_CATEGORIES = [
    None,  # default desktop voices
    r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices",
]


def _iter_tokens(client):
    for cat_id in _SAPI_CATEGORIES:
        try:
            if cat_id is None:
                tokens = client.Dispatch("SAPI.SpVoice").GetVoices()
            else:
                cat = client.Dispatch("SAPI.SpObjectTokenCategory")
                cat.SetId(cat_id, False)
                tokens = cat.EnumerateTokens()
            for i in range(tokens.Count):
                yield tokens.Item(i)
        except Exception:
            continue


def _find_token(client, token_id):
    for t in _iter_tokens(client):
        try:
            if t.Id == token_id:
                return t
        except Exception:
            pass
    return None


def list_sapi_voices() -> list[SapiVoice]:
    if sys.platform != "win32":
        return []
    try:
        import pythoncom
        pythoncom.CoInitialize()
        client = _sapi_dispatch()
    except Exception:
        return []
    voices, seen = [], set()
    for t in _iter_tokens(client):
        try:
            desc = t.GetDescription()
            tid = t.Id
        except Exception:
            continue
        if desc in seen:
            continue
        seen.add(desc)
        voices.append(SapiVoice(f"{desc} (Windows, offline)", tid))

    def rank(v):
        d = v.label.lower()
        indian = "india" in d or "heera" in d or "ravi" in d or "en-in" in d
        female = "heera" in d or "female" in d or "zira" in d
        return (0 if indian and female else 1 if indian else 2 if female else 3, d)

    voices.sort(key=rank)
    return voices


def all_voices():
    return [EdgeVoice(lbl, vid) for lbl, vid in EDGE_VOICES] + list_sapi_voices()
