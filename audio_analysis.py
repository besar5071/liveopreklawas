"""Local PCM/spectral analysis for Live audio samples (heuristics, not song recognition)."""
from __future__ import annotations

import math
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


SAMPLE_RATE = 16_000
SAMPLE_SECONDS = 20
MAX_TRACKS = 3


def _pcm_sample(path: Path, seconds: int = SAMPLE_SECONDS) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg tidak tersedia untuk menganalisis audio.")
    try:
        result = subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
            "-map", "0:a:0", "-t", str(seconds), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-f", "s16le", "pipe:1",
        ], capture_output=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Gagal membaca sampel audio {path.name}: {exc}") from exc
    if result.returncode or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"FFmpeg gagal membaca {path.name}: {detail or 'audio kosong'}")
    return result.stdout


def analyze_audio(paths: list[str | Path], output_dir: str | Path,
                  max_tracks: int = MAX_TRACKS) -> dict:
    """Decode short PCM excerpts and calculate interpretable spectral descriptors."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    tracks = []
    errors = []
    for idx, raw_path in enumerate(paths[:max(1, min(int(max_tracks), MAX_TRACKS))]):
        path = Path(raw_path)
        try:
            pcm = _pcm_sample(path)
            x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            x = x[:SAMPLE_RATE * SAMPLE_SECONDS]
            if len(x) < 1024:
                raise RuntimeError("sampel audio terlalu pendek untuk analisis")

            hop, nfft = 512, 1024
            frames = np.lib.stride_tricks.sliding_window_view(x, nfft)[::hop]
            spectrum = np.abs(np.fft.rfft(frames * np.hanning(nfft), axis=1)).T
            power = spectrum ** 2
            frequencies = np.fft.rfftfreq(nfft, 1.0 / SAMPLE_RATE)
            frame_power = np.sum(power, axis=0) + 1e-10
            centroid = float(np.mean((power * frequencies[:, None]).sum(axis=0) / frame_power))
            rolloff_bins = np.argmax(np.cumsum(power, axis=0) >= 0.85 * frame_power, axis=0)
            rolloff = float(np.mean(frequencies[rolloff_bins]))
            flux = float(np.mean(np.maximum(0.0, np.diff(spectrum, axis=1)))) if spectrum.shape[1] > 1 else 0.0
            rms = float(np.sqrt(np.mean(x * x)))
            zcr = float(np.mean(np.abs(np.diff(np.sign(x))) > 0))

            # Onset-envelope autocorrelation gives only a rough tempo estimate.
            onset = np.maximum(0.0, np.diff(frame_power))
            onset -= onset.mean() if onset.size else 0.0
            autocorrelation = np.correlate(onset, onset, mode="full")[len(onset) - 1:] if onset.size else np.array([])
            low = int((60 / 180) * SAMPLE_RATE / hop)
            high = min(len(autocorrelation) - 1, int((60 / 65) * SAMPLE_RATE / hop))
            tempo = round(60 * SAMPLE_RATE / hop / (low + int(np.argmax(autocorrelation[low:high + 1]))), 1) \
                if high > low else None

            db = 20 * np.log10(spectrum[::-1] + 1e-5)
            normalized = np.clip((db + 70.0) / 70.0 * 255.0, 0, 255).astype(np.uint8)
            rgb = np.zeros((*normalized.shape, 3), dtype=np.uint8)
            rgb[:, :, 0] = normalized
            rgb[:, :, 1] = (normalized * 0.65).astype(np.uint8)
            rgb[:, :, 2] = (200 - normalized // 2).astype(np.uint8)
            plot = Image.fromarray(rgb, mode="RGB").resize((900, 256))
            panel = Image.new("RGB", (900, 300), "#101820")
            panel.paste(plot, (0, 34))
            ImageDraw.Draw(panel).text(
                (14, 9), f"PCM 16 kHz · 0–8 kHz · {len(x) / SAMPLE_RATE:.1f}s · {path.name[:72]}",
                fill="white",
            )
            image_path = output_dir / f"spectrogram-{idx}.png"
            panel.save(image_path, format="PNG", optimize=True)
            tracks.append({
                "filename": path.name,
                "rms_dbfs": round(20 * math.log10(max(rms, 1e-8)), 2),
                "zero_crossing_rate": round(zcr, 4),
                "spectral_centroid_hz": round(centroid),
                "rolloff_85_hz": round(rolloff),
                "spectral_flux": round(flux, 5),
                "tempo_estimate_bpm": tempo,
                "sample_seconds": round(len(x) / SAMPLE_RATE, 2),
                "spectrogram": str(image_path),
            })
        except Exception as exc:  # process remaining playlist items even if one is invalid
            errors.append({"filename": path.name, "error": str(exc)[:300]})

    if not tracks:
        reason = "; ".join(f"{x['filename']}: {x['error']}" for x in errors[:3])
        raise RuntimeError("Tidak ada audio yang berhasil dianalisis." + (f" {reason}" if reason else ""))
    return {
        "method": "ffmpeg PCM mono 16 kHz + STFT nfft=1024/hop=512; spectral descriptors and estimated tempo",
        "disclaimer": "Fitur spektral dan tempo adalah heuristik, bukan identifikasi lagu, artis, lirik, atau genre yang pasti.",
        "sample_seconds_per_track": SAMPLE_SECONDS,
        "tracks": tracks,
        "errors": errors,
        "generated_at": time.time(),
    }
