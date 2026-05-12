"""
Lokalna transkrypcja audio przez whisper.cpp (whisper-cli) — Sprint 1.8.

Dlaczego lokalny zamiast OpenAI Whisper API:
- Zero kosztu (vs $0.006/min OpenAI)
- Brak network round-trip (szybciej dla krótkich voice memo)
- Privacy (audio nie opuszcza Maca Grzegorza)
- whisper.cpp z ggml-large model = jakość transkrypcji jak OpenAI Whisper-1
- whisper.cpp już zainstalowany lokalnie (CLAUDE.md user_macguard_dev.md)

Wymaga:
- whisper-cli w PATH (brew install whisper-cpp)
- Model ggml-*.bin (default: /Applications/Aiko.app/Contents/Resources/ggml-large.bin)
- ffmpeg w PATH (konwersja OGG opus → WAV 16kHz mono PCM)

Override przez env:
- WHISPER_MODEL_PATH — ścieżka do ggml-*.bin (default Aiko large)
- WHISPER_LANGUAGE — kod języka (default "pl"; "auto" dla detection)
- WHISPER_CLI — ścieżka do binarki (default whisper-cli z PATH)

Flow:
  bytes (OGG opus z Telegrama)
    → tmp .ogg
    → ffmpeg → tmp .wav (16kHz, 1ch, PCM 16-bit)
    → whisper-cli -m <model> -f <wav> -l <lang> -otxt
    → read tmp .wav.txt
    → cleanup
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


DEFAULT_MODEL_CANDIDATES = [
    os.environ.get("WHISPER_MODEL_PATH", ""),
    "/Applications/Aiko.app/Contents/Resources/ggml-large.bin",
    "/usr/local/share/whisper.cpp/ggml-large.bin",
    str(Path.home() / "models" / "ggml-large.bin"),
]


@dataclass
class TranscribeResult:
    success: bool
    text: str = ""
    duration_sec: float = 0.0
    model_used: str = ""
    error: Optional[str] = None


def _find_model() -> Optional[str]:
    for p in DEFAULT_MODEL_CANDIDATES:
        if p and Path(p).exists() and Path(p).stat().st_size > 1024 * 1024:
            return p
    return None


def _find_whisper_cli() -> Optional[str]:
    explicit = os.environ.get("WHISPER_CLI")
    if explicit and Path(explicit).exists():
        return explicit
    # Sprawdź PATH
    found = shutil.which("whisper-cli") or shutil.which("whisper")
    return found


def _convert_to_wav_sync(input_path: str, output_path: str) -> tuple[bool, str]:
    """ffmpeg: OGG/MP3/M4A → WAV 16kHz mono PCM (format wymagany przez Whisper)."""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-i", input_path,
                "-ar", "16000",
                "-ac", "1",
                "-c:a", "pcm_s16le",
                "-y",
                "-loglevel", "error",
                output_path,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timeout 60s"
    except subprocess.CalledProcessError as e:
        return False, f"ffmpeg fail: {(e.stderr or '')[:300]}"
    except FileNotFoundError:
        return False, "ffmpeg nie zainstalowany"


def _run_whisper_sync(
    wav_path: str,
    model_path: str,
    cli_path: str,
    language: str,
) -> tuple[bool, str, str]:
    """whisper-cli generuje <wav_path>.txt z transkrypcją."""
    args = [
        cli_path,
        "-m", model_path,
        "-f", wav_path,
        "-l", language,
        "--no-prints",
        "-otxt",
    ]
    try:
        subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,  # 5 min max
        )
        # output file: <wav_path>.txt
        txt_path = wav_path + ".txt"
        if not Path(txt_path).exists():
            return False, "", "whisper-cli .txt output nie powstał"
        text = Path(txt_path).read_text(encoding="utf-8")
        try:
            Path(txt_path).unlink()
        except Exception:
            pass
        return True, text.strip(), ""
    except subprocess.TimeoutExpired:
        return False, "", "whisper timeout 300s"
    except subprocess.CalledProcessError as e:
        return False, "", f"whisper fail: {(e.stderr or '')[:300]}"


async def transcribe(
    audio_bytes: bytes,
    *,
    source_extension: str = ".ogg",
    language: Optional[str] = None,
) -> TranscribeResult:
    """
    Transkrybuje audio (Telegram voice = OGG opus) lokalnie przez whisper.cpp.

    Args:
        audio_bytes: surowe bajty audio
        source_extension: rozszerzenie sugerowane przez Telegram (".ogg" zwykle)
        language: kod języka ("pl", "en", "auto"). Default env WHISPER_LANGUAGE lub "pl".

    Returns:
        TranscribeResult(success, text, ...)
    """
    cli_path = _find_whisper_cli()
    if not cli_path:
        return TranscribeResult(
            success=False,
            error="whisper-cli nie znalezione. Zainstaluj: brew install whisper-cpp",
        )

    model_path = _find_model()
    if not model_path:
        return TranscribeResult(
            success=False,
            error=(
                "Model Whisper (ggml-*.bin) nie znaleziony. Sprawdź ścieżki: "
                f"{', '.join(p for p in DEFAULT_MODEL_CANDIDATES if p)}. "
                "Albo ustaw env WHISPER_MODEL_PATH=<path-to-ggml-large.bin>"
            ),
        )

    lang = language or os.environ.get("WHISPER_LANGUAGE", "pl")

    with tempfile.TemporaryDirectory(prefix="ulos_whisper_") as tmp:
        in_path = os.path.join(tmp, f"input{source_extension}")
        wav_path = os.path.join(tmp, "audio.wav")
        Path(in_path).write_bytes(audio_bytes)

        # ffmpeg conversion (w threadpoolu — sync)
        ok, err = await asyncio.to_thread(_convert_to_wav_sync, in_path, wav_path)
        if not ok:
            return TranscribeResult(success=False, error=err)

        # whisper-cli (w threadpoolu — sync, może trwać sekundami)
        ok, text, err = await asyncio.to_thread(
            _run_whisper_sync, wav_path, model_path, cli_path, lang
        )
        if not ok:
            return TranscribeResult(success=False, error=err)

        # Czyszczenie tekstu (Whisper czasem zostawia początkowe spacje, znaki czasu w nawiasach)
        # Usuwamy puste linie + nadmiarowe spacje
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        clean = "\n".join(lines)

        if not clean or clean.startswith("[") and "BLANK" in clean.upper():
            return TranscribeResult(
                success=False,
                text="",
                model_used=Path(model_path).name,
                error="Transkrypcja pusta (BLANK_AUDIO)",
            )

        return TranscribeResult(
            success=True,
            text=clean,
            model_used=Path(model_path).name,
        )
