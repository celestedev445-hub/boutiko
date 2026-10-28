"""Convert WhatsApp voice notes (OGG/Opus) to WAV PCM 16 kHz mono 16-bit with ffmpeg."""

import io
import shutil
import subprocess
import wave
from dataclasses import dataclass

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # bytes, 16-bit
CHANNELS = 1
MAX_SECONDS = 300
TIMEOUT_SECONDS = 30


class AudioConversionError(RuntimeError):
    pass


class AudioTooLongError(RuntimeError):
    pass


@dataclass
class WavAudio:
    data: bytes  # complete WAV file
    duration_seconds: float


def convert_to_wav(audio: bytes, max_seconds: int = MAX_SECONDS) -> WavAudio:
    """Decode any audio ffmpeg understands. Raises AudioTooLongError beyond max_seconds."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AudioConversionError("ffmpeg not found")

    # Raw PCM on stdout, WAV header written here: through a pipe ffmpeg can't go
    # back to fill in the WAV sizes. "-t" stops decoding just past the limit.
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", "pipe:0",
        "-t", str(max_seconds + 1),
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-acodec", "pcm_s16le",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(command, input=audio, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AudioConversionError(f"ffmpeg timed out after {TIMEOUT_SECONDS} s") from exc
    except OSError as exc:
        raise AudioConversionError(f"ffmpeg could not start: {exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode(errors="replace").strip()[-300:]
        raise AudioConversionError(f"ffmpeg exited with {completed.returncode}: {stderr}")

    pcm = completed.stdout
    duration = len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
    if duration > max_seconds:
        raise AudioTooLongError(f"more than {max_seconds} s")
    return WavAudio(data=_wav(pcm), duration_seconds=duration)


def _wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()
