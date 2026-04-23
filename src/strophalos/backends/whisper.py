"""Whisper transcription — POST audio to external Whisper ASR container.

The Whisper service (onerahmet/openai-whisper-asr-webservice) accepts any
audio/video file and returns timestamped transcription.  We extract the
audio track with ffmpeg, send it, and parse the SRT response into
(timestamp, text) pairs compatible with the subtitle scoring pipeline.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from strophalos.identify.srt import parse_srt


def transcribe(
    mkv_path: Path,
    duration: float,
    *,
    url: str | None = None,
    language: str = "en",
) -> list[tuple[float, str]]:
    """Transcribe audio from an MKV file via the external Whisper service.

    Extracts the full audio as WAV, POSTs it to the Whisper /asr endpoint,
    parses the SRT response, and returns all (timestamp_seconds, text) pairs.

    Returns [] if the Whisper service is not configured or on any failure.
    """
    whisper_url = url or os.environ.get("WHISPER_URL", "")
    if not whisper_url:
        return []

    # Extract audio as WAV (mono 16kHz — what Whisper expects)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        wav_path = tmp.name

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(mkv_path),
                "-ac", "1", "-ar", "16000",
                "-vn", "-f", "wav", wav_path,
            ],
            capture_output=True,
            timeout=300,
        )
    except Exception as e:
        print(f"    whisper: ffmpeg extraction failed: {e}")
        return []

    wav = Path(wav_path)
    if not wav.exists() or wav.stat().st_size == 0:
        return []

    try:
        results = _post_asr(whisper_url, wav, language)
    finally:
        wav.unlink(missing_ok=True)

    if results:
        print(f"    whisper: {len(results)} cue(s) from transcription")

    return results


def _post_asr(
    base_url: str,
    wav_path: Path,
    language: str,
) -> list[tuple[float, str]]:
    """POST a WAV file to the Whisper ASR endpoint, return parsed SRT cues."""
    endpoint = f"{base_url.rstrip('/')}/asr"
    params = urllib.parse.urlencode({
        "output": "srt",
        "language": language,
        "encode": "false",  # already WAV
        "vad_filter": "true",
    })
    url = f"{endpoint}?{params}"

    # Build multipart/form-data manually (no requests dependency)
    boundary = "----StrophalosWhisperBoundary"
    audio_data = wav_path.read_bytes()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="audio_file"; filename="{wav_path.name}"\r\n'
        f"Content-Type: audio/wav\r\n"
        f"\r\n"
    ).encode() + audio_data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            srt_text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"    whisper: ASR request failed: {e}")
        return []

    return parse_srt(srt_text)
