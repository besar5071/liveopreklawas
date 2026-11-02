"""Pembuat video dari teks (text-to-video) untuk Studio.

Alur kerja:

    ide / teks  →  (opsional) draf skenario via relay.fast (chat OpenAI-compatible)
                →  narasi TTS per adegan
                →  gambar adegan (Pillow: gradient / geometris / editorial)
                →  FFmpeg: zoom-pan + fade + subtitle + musik latar
                →  MP4 di Library → Hasil

relay.fast menyediakan endpoint gaya OpenAI (`/v1/chat/completions`), jadi dipakai
untuk menulis skenario, judul, dan teks layar. Video sendiri dirakit di server ini
dengan Pillow + FFmpeg, sehingga proses tetap berjalan walau provider tidak
menyediakan model video, dan tidak ada media yang dikirim keluar.

Semua fungsi publik memakai tipe sederhana (dict/list/Path) agar mudah diuji.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import requests

DATA_DIR = Path("data").resolve()
OUTPUT_DIR = DATA_DIR / "output"
TMP_DIR = DATA_DIR / "tmp"
PLAN_DIR = DATA_DIR / "plans"

RELAY_BASE_URL = os.environ.get("RELAY_BASE_URL", "https://relay.fast/v1").rstrip("/")
RELAY_API_KEY = os.environ.get("RELAY_API_KEY", "")
RELAY_MODEL = os.environ.get("RELAY_MODEL", "gpt-6-astra")
RELAY_RESERVE_BASE_URL = os.environ.get("RELAY_RESERVE_BASE_URL",
                                        "https://direct.relay-fast.com/v1").rstrip("/")

ASPECTS = {
    "9:16 · Shorts/Reels": "9:16",
    "1:1 · Kotak": "1:1",
    "16:9 · YouTube": "16:9",
}
QUALITIES = {"Cepat · 720×1280": "fast", "Tinggi · 1080×1920": "high"}
CANVAS = {
    ("9:16", "fast"): (720, 1280),
    ("9:16", "high"): (1080, 1920),
    ("1:1", "fast"): (720, 720),
    ("1:1", "high"): (1080, 1080),
    ("16:9", "fast"): (1280, 720),
    ("16:9", "high"): (1920, 1080),
}
TONES = {
    "Viral / Hook cepat": "viral",
    "Edukasi": "edukasi",
    "Promosi": "promosi",
    "Santai / bercerita": "santai",
}
STYLES = {
    "Gradient Neon": "gradient",
    "Geometris Bold": "geometric",
    "Editorial Terang": "editorial",
}
PALETTES = [
    {"bg": ("#0f172a", "#1e3a8a"), "accent": "#22d3ee", "ink": "#f8fafc"},
    {"bg": ("#1a0b2e", "#4c1d95"), "accent": "#f472b6", "ink": "#fdf4ff"},
    {"bg": ("#0b1120", "#065f46"), "accent": "#4ade80", "ink": "#f0fdf4"},
    {"bg": ("#1c1917", "#7c2d12"), "accent": "#fb923c", "ink": "#fff7ed"},
    {"bg": ("#f8fafc", "#e2e8f0"), "accent": "#2563eb", "ink": "#0f172a"},
    {"bg": ("#111827", "#be123c"), "accent": "#fbbf24", "ink": "#fffbeb"},
]
VOICES = [
    ("id-ID-ArdiNeural", "🇮🇩 Pria · Ardi"),
    ("id-ID-GadisNeural", "🇮🇩 Wanita · Gadis"),
    ("en-US-AndrewNeural", "🇺🇸 Pria · Andrew"),
    ("en-US-AvaNeural", "🇺🇸 Wanita · Ava"),
    ("ms-My-OsmanNeural", "🇲🇾 Pria · Osman"),
]
DEFAULT_VOICE = VOICES[0][0]
DEFAULT_FPS = 30
SCENE_PAD = 0.7          # jeda diam setelah narasi (detik)
MIN_SCENE_SECONDS = 3.0
MAX_SCENE_SECONDS = 45.0
MAX_SCENES = 10

FONT_CANDIDATES = {
    "sans": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ],
    "sans_bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ],
    "serif": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    ],
    "serif_bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    ],
}


class RelayVideoError(RuntimeError):
    """Kesalahan yang pesannya aman ditampilkan ke pengguna."""


# ───────────────────────── util umum ─────────────────────────
def ffmpeg_bin() -> str:
    """Lokasi ffmpeg aplikasi, atau bundel dari paket imageio-ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise RelayVideoError(
            "ffmpeg tidak ditemukan. Pastikan `packages.txt` berisi `ffmpeg`."
        ) from exc


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=str(cwd) if cwd else None)
    except subprocess.TimeoutExpired as exc:
        raise RelayVideoError(f"Perintah ffmpeg melebihi batas waktu {timeout}s.") from exc


def media_duration(path: str | Path) -> float:
    """Durasi media (detik) via ffprobe, dengan fallback parsing ffmpeg."""
    path = Path(path)
    probe = shutil.which("ffprobe")
    if probe:
        res = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", str(path)],
                             capture_output=True, text=True, timeout=60)
        try:
            value = float((res.stdout or "").strip())
            if value > 0:
                return value
        except ValueError:
            pass
    res = _run([ffmpeg_bin(), "-hide_banner", "-i", str(path)], timeout=120)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def has_audio_stream(path: str | Path) -> bool:
    """True bila media punya track audio (fallback: parsing stderr ffmpeg)."""
    path = Path(path)
    probe = shutil.which("ffprobe")
    if probe:
        res = subprocess.run([probe, "-v", "error", "-select_streams", "a",
                              "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=60)
        if res.returncode == 0:
            return bool((res.stdout or "").strip())
    try:
        res = _run([ffmpeg_bin(), "-hide_banner", "-i", str(path)], timeout=120)
    except RelayVideoError:
        return False
    return "Audio:" in (res.stderr or "")


def _note(job: dict | None, message: str) -> None:
    """Tulis progres ke job supaya panel status Streamlit ikut berubah."""
    if isinstance(job, dict):
        job["detail"] = str(message)[:600]


# ───────────────────────── warna & teks ─────────────────────────
def _hex(value: str, fallback: str = "#22d3ee") -> str:
    candidate = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", candidate):
        return candidate.lower()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", candidate):
        return "#" + "".join(c * 2 for c in candidate[1:].lower())
    return fallback


def _rgb(value: str) -> tuple[int, int, int]:
    value = _hex(value).lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(a: str, b: str, t: float) -> tuple[int, int, int]:
    ra, rb = _rgb(a), _rgb(b)
    t = max(0.0, min(1.0, t))
    return tuple(int(round(ra[i] + (rb[i] - ra[i]) * t)) for i in range(3))  # type: ignore[return-value]


def _luminance(color: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        value = value / 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def readable_ink(background: tuple[int, int, int], preferred: str) -> str:
    """Pilih warna teks yang kontras terhadap latar (WCAG-ish sederhana)."""
    lum_bg = _luminance(background)
    light, dark = "#ffffff", "#0b1120"
    contrast = lambda a, b: (max(a, b) + 0.05) / (min(a, b) + 0.05)  # noqa: E731
    candidates = [preferred, light, dark]
    scored = [(contrast(_luminance(_rgb(_hex(c, light))), lum_bg), c) for c in candidates]
    best = max(scored)[0]
    for score, color in scored:
        if color == preferred and score >= 4.5:
            return _hex(preferred)
    for score, color in scored:
        if score == best:
            return color
    return light


EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF" "\U0000FE00-\U0000FE0F" "\U0001F1E6-\U0001F1FF" "]+"
)


def clean_text(value: Any, limit: int = 2000) -> str:
    """Buang emoji/markdown agar font DejaVu tidak menampilkan kotak kosong."""
    text = str(value or "")
    text = EMOJI_RE.sub("", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[-*•]\s+", "", text)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()[:limit]


def split_sentences(text: str) -> list[str]:
    cleaned = clean_text(text, 6000)
    parts = re.split(r"(?<=[.!?])\s+|\n+", cleaned)
    return [p.strip() for p in parts if p.strip()]


# ───────────────────────── relay.fast (LLM) ─────────────────────────
def relay_headers(api_key: str) -> dict:
    key = (api_key or "").strip()
    if not key:
        raise RelayVideoError("API key relay.fast belum diisi.")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def list_models(api_key: str, base_url: str = RELAY_BASE_URL, timeout: int = 30) -> list[str]:
    """Daftar id model dari /v1/models (kosong bila provider tidak mendukung)."""
    try:
        res = requests.get(f"{(base_url or RELAY_BASE_URL).rstrip('/')}/models",
                           headers=relay_headers(api_key), timeout=timeout)
    except requests.RequestException as exc:
        raise RelayVideoError(f"Tidak dapat menghubungi relay.fast: {exc}") from exc
    if not res.ok:
        raise RelayVideoError(f"relay.fast menolak permintaan ({res.status_code}): {res.text[:300]}")
    try:
        payload = res.json()
    except ValueError as exc:
        raise RelayVideoError("Respons daftar model relay.fast bukan JSON.") from exc
    items = payload.get("data") if isinstance(payload, dict) else payload
    models: list[str] = []
    for item in items or []:
        if isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
        elif isinstance(item, str):
            models.append(item)
    return sorted(dict.fromkeys(models))


def chat(api_key: str, prompt: str, system: str, model: str = RELAY_MODEL,
         base_url: str = RELAY_BASE_URL, timeout: int = 180,
         temperature: float = 0.85) -> str:
    """Satu panggilan chat completion gaya OpenAI, dengan endpoint cadangan."""
    headers = relay_headers(api_key)
    body = {
        "model": model or RELAY_MODEL,
        "temperature": temperature,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }
    errors: list[str] = []
    for candidate in dict.fromkeys([base_url or RELAY_BASE_URL, RELAY_RESERVE_BASE_URL]):
        try:
            res = requests.post(f"{candidate.rstrip('/')}/chat/completions", headers=headers,
                                json=body, timeout=timeout)
        except requests.RequestException as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        if not res.ok:
            errors.append(f"{candidate} ({res.status_code}): {res.text[:300]}")
            continue
        try:
            payload = res.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            errors.append(f"{candidate}: format respons tidak dikenali")
            continue
        if isinstance(content, list):  # beberapa provider mengembalikan list part
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if str(content or "").strip():
            return str(content)
        errors.append(f"{candidate}: jawaban kosong")
    raise RelayVideoError("Gagal meminta skenario ke relay.fast. " + " | ".join(errors)[:600])


def json_from_text(text: str) -> dict:
    """Ambil objek JSON pertama dari jawaban model (boleh dibungkus prosa/markdown)."""
    if not isinstance(text, str) or not text.strip():
        raise RelayVideoError("Model mengembalikan jawaban kosong, bukan JSON.")
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.I)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            data, _ = decoder.raw_decode(cleaned[index:])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    raise RelayVideoError("Respons relay.fast tidak berisi objek JSON yang valid.")


PLAN_SYSTEM = (
    "Kamu adalah penulis naskah video pendek berbahasa Indonesia yang ringkas, konkret, "
    "dan enak didengar saat dibacakan mesin TTS. Selalu jawab HANYA dengan objek JSON valid."
)


def tone_key(tone: str | None) -> str:
    """Terima kunci (`edukasi`) maupun label UI (`Edukasi`)."""
    value = str(tone or "").strip()
    if value in TONES:
        return TONES[value]
    lowered = value.lower()
    for key in TONES.values():
        if lowered == key:
            return key
    return "viral"


def plan_prompt(topic: str, scenes: int, duration: int, language: str = "id",
                tone: str = "viral", audience: str = "") -> str:
    tone_map = {"viral": "cepat, memancing rasa penasaran, hook kuat di awal",
                "edukasi": "jelas dan runtut seperti penjelasan guru",
                "promosi": "meyakinkan dengan manfaat dan ajakan bertindak",
                "santai": "hangat, akrab, seperti bercerita ke teman"}
    tone = tone_key(tone)
    return (
        f"Buat naskah video pendek {duration} detik dari ide berikut:\n\"\"\"{clean_text(topic, 4000)}\"\"\"\n\n"
        f"Bahasa: {'Indonesia' if language == 'id' else 'Inggris'}.\n"
        f"Jumlah adegan: tepat {scenes}.\n"
        f"Gaya penyampaian: {tone_map.get(tone, tone_map['viral'])}.\n"
        + (f"Audiens: {audience}.\n" if audience else "")
        + "Aturan: setiap adegan punya 1-2 kalimat narasi (maksimal 240 karakter), "
        "headline pendek maksimal 6 kata untuk ditampilkan di layar, subheadline maksimal 90 karakter, "
        "1-3 poin singkat (maksimal 48 karakter per poin), dan satu warna aksen hex.\n"
        "Jangan memakai emoji, markdown, atau hashtag di dalam teks layar.\n"
        "Format JSON:\n"
        '{"title": "judul video maksimal 60 karakter", "voice": "id-ID-ArdiNeural", '
        '"scenes": [{"narration": "...", "headline": "...", "subheadline": "...", '
        '"bullets": ["..."], "accent": "#22d3ee", "image_query": "kata kunci visual bahasa Inggris"}]}'
    )


def normalize_plan(plan: dict, topic: str = "", max_scenes: int = MAX_SCENES) -> dict:
    """Rapikan JSON dari model menjadi skenario yang pasti bisa dirender."""
    if not isinstance(plan, dict):
        raise RelayVideoError("Skenario bukan objek JSON.")
    raw_scenes = plan.get("scenes")
    if not isinstance(raw_scenes, list):
        raw_scenes = []
    scenes: list[dict] = []
    for index, raw in enumerate(raw_scenes[:max_scenes]):
        if not isinstance(raw, dict):
            if isinstance(raw, str) and raw.strip():
                raw = {"narration": raw}
            else:
                continue
        narration = clean_text(raw.get("narration") or raw.get("text") or "", 700)
        headline = clean_text(raw.get("headline") or raw.get("title") or "", 90)
        subheadline = clean_text(raw.get("subheadline") or raw.get("subtitle") or "", 180)
        bullets_raw = raw.get("bullets") or raw.get("points") or []
        if isinstance(bullets_raw, str):
            bullets_raw = re.split(r"[|;\n]+", bullets_raw)
        bullets = [clean_text(b, 70) for b in bullets_raw if clean_text(b, 70)][:3]
        if not narration and not headline and not bullets:
            continue
        if not headline:
            headline = narration.split(",")[0][:60] or f"Adegan {index + 1}"
        if not narration:
            narration = subheadline or headline
        scenes.append({
            "narration": narration,
            "headline": headline,
            "subheadline": subheadline,
            "bullets": bullets,
            "accent": _hex(raw.get("accent"), ""),
            "image_query": clean_text(raw.get("image_query") or "", 120),
        })
    if not scenes:
        source = clean_text(topic or "", 2000) or clean_text(plan.get("title") or "", 200)
        if not source:
            raise RelayVideoError("Skenario tidak memiliki adegan; isi ide/teks atau minta draf ulang.")
        return fallback_plan(source)
    title = clean_text(plan.get("title") or "", 90) or (scenes[0]["headline"] or "Video dari teks")
    voice = str(plan.get("voice") or "").strip()
    return {"title": title, "voice": voice if re.fullmatch(r"[\w-]+Neural", voice) else "",
            "scenes": scenes}


def fallback_plan(topic: str, scenes: int = 4) -> dict:
    """Skenario tanpa LLM: teks pengguna langsung menjadi narasi."""
    sentences = split_sentences(topic) or ["Tambahkan ide atau teks untuk dijadikan video."]
    scenes = max(1, min(MAX_SCENES, int(scenes or 4)))
    # Kelompokkan kalimat agar tiap adegan cukup berisi tanpa terlalu panjang.
    groups: list[list[str]] = [[] for _ in range(min(scenes, len(sentences)))] or [[]]
    for index, sentence in enumerate(sentences):
        groups[index % len(groups)].append(sentence)
    result = []
    for index, group in enumerate(groups):
        narration = " ".join(group)[:500]
        words = narration.split()
        if len(words) <= 14:
            # Cukup pendek: seluruh kalimat menjadi judul layar (pembungkus teks
            # akan mengecilkan font sampai muat), tanpa subjudul terpotong.
            headline, rest = " ".join(words).rstrip(",.;:"), ""
        else:
            headline = " ".join(words[:10]).rstrip(",.;:")
            rest = " ".join(words[10:])[:140]
        headline = headline or f"Bagian {index + 1}"
        palette = PALETTES[index % len(PALETTES)]
        result.append({
            "narration": narration,
            "headline": headline,
            "subheadline": rest,
            "bullets": [],
            "accent": palette["accent"],
            "image_query": "",
        })
    title = sentences[0][:70] if sentences else "Video dari teks"
    return {"title": title, "voice": "", "scenes": result}


def draft_plan(api_key: str, topic: str, model: str = RELAY_MODEL, scenes: int = 4,
               duration: int = 30, language: str = "id", tone: str = "viral",
               audience: str = "", base_url: str = RELAY_BASE_URL) -> dict:
    """Minta relay.fast menulis skenario, lalu normalisasi hasilnya."""
    if not clean_text(topic):
        raise RelayVideoError("Isi dulu ide atau teks untuk dijadikan video.")
    prompt = plan_prompt(topic, scenes, duration, language, tone, audience)
    answer = chat(api_key, prompt, PLAN_SYSTEM, model=model, base_url=base_url)
    return normalize_plan(json_from_text(answer), topic=topic, max_scenes=scenes)


# ───────────────────────── suara (TTS) ─────────────────────────
def default_voice(value: str | None = None) -> str:
    voice = str(value or "").strip()
    return voice if re.fullmatch(r"[\w-]+Neural", voice) else DEFAULT_VOICE


def synthesize_speech(text: str, out_path: str | Path, voice: str = DEFAULT_VOICE,
                      rate: str = "+0%") -> Path:
    """Buat satu berkas MP3 dari teks memakai edge-tts (gratis, tanpa API key)."""
    spoken = clean_text(text, 2000)
    if not spoken:
        raise RelayVideoError("Teks narasi kosong, tidak ada yang bisa dibacakan.")
    try:
        import asyncio
        import edge_tts
    except ImportError as exc:  # noqa: BLE001
        raise RelayVideoError(
            "edge-tts belum terpasang. Tambahkan `edge-tts` ke requirements.txt, "
            "atau render tanpa narasi."
        ) from exc
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    communicate = edge_tts.Communicate(spoken, default_voice(voice), rate=rate or "+0%")

    async def _save() -> None:
        await communicate.save(str(out))

    try:
        asyncio.run(_save())
    except RuntimeError:
        # Sudah ada event loop berjalan (mis. di dalam thread Streamlit tertentu).
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_save())
        finally:
            loop.close()
    except Exception as exc:  # noqa: BLE001
        hint = "Periksa koneksi internet server (edge-tts memakai layanan Microsoft)."
        raise RelayVideoError(f"Gagal membuat suara TTS: {exc}. {hint}") from exc
    if not out.is_file() or out.stat().st_size < 512:
        raise RelayVideoError("TTS menghasilkan berkas kosong; coba suara lain.")
    return out


# ───────────────────────── gambar adegan ─────────────────────────
def canvas_size(aspect: str, quality: str = "high") -> tuple[int, int]:
    key = ASPECTS.get(aspect, ASPECTS["9:16 · Shorts/Reels"])
    return CANVAS.get((key, QUALITIES.get(quality, "high")), CANVAS[(key, "high")])


def _font(bold: bool = False, serif: bool = False, size: int = 64):
    from PIL import ImageFont

    key = ("serif" if serif else "sans") + ("_bold" if bold else "")
    for path in FONT_CANDIDATES.get(key, []):
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    # Fallback: font DejaVu yang ikut paket matplotlib (biasanya selalu terpasang).
    try:
        import matplotlib

        bundled = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        name = ("DejaVuSerif" if serif else "DejaVuSans") + ("-Bold" if bold else "") + ".ttf"
        if (bundled / name).is_file():
            return ImageFont.truetype(str(bundled / name), size=size)
    except Exception:  # noqa: BLE001
        pass
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text).splitlines() or [""]:
        if not paragraph.strip():
            continue
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}".strip()
            if line and draw.textlength(candidate, font=font) > max_width:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
    return lines


def fit_text(draw, text: str, max_width: int, size: int, min_size: int = 22,
             bold: bool = False, serif: bool = False, max_lines: int = 6):
    """Perkecil font sampai teks muat pada lebar dan jumlah baris tertentu."""
    size = max(min_size, int(size))
    while True:
        font = _font(bold=bold, serif=serif, size=size)
        lines = _wrap(draw, text, font, max_width)
        if (len(lines) <= max_lines and all(draw.textlength(l, font=font) <= max_width for l in lines)) \
                or size <= min_size:
            return font, lines
        size -= 2


def fonts_dir() -> str:
    """Folder font yang berisi DejaVu Sans, untuk opsi fontsdir libass."""
    candidates = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF",
                  "/usr/local/share/fonts/truetype/dejavu", "/Library/Fonts"]
    try:
        import matplotlib

        candidates.append(str(Path(matplotlib.get_data_path()) / "fonts" / "ttf"))
    except Exception:  # noqa: BLE001
        pass
    for candidate in candidates:
        folder = Path(candidate)
        if (folder / "DejaVuSans.ttf").is_file():
            return str(folder)
    return ""


def _blobs_available() -> bool:
    """Cek numpy tanpa mengimpornya di level modul (impor bersifat opsional)."""
    import importlib.util

    return importlib.util.find_spec("numpy") is not None


def _background(style: str, palette: dict, size: tuple[int, int], seed: int):
    """Latar per adegan: gradient halus, geometris tebal, atau editorial terang."""
    from PIL import Image, ImageDraw, ImageFilter

    width, height = size
    random.seed(seed)
    bg_a, bg_b = palette["bg"]
    accent = palette["accent"]

    if style == "editorial":
        # Editorial selalu terang: kertas putih dengan tint tipis dari palet adegan.
        paper = _mix("#f8fafc", bg_b, 0.08)
        base = Image.new("RGB", size, paper)
        draw = ImageDraw.Draw(base)
        draw.rectangle((0, int(height * 0.88), width, height), fill=_mix(accent, "#0b1120", 0.12))
        draw.rectangle((0, 0, width, int(height * 0.014)), fill=_mix(accent, "#0b1120", 0.12))
        for index in range(6):
            y = int(height * (0.14 + index * 0.11))
            draw.line((int(width * 0.08), y, int(width * 0.92), y),
                      fill=_mix("#cbd5e1", paper, 0.35), width=2)
        return base

    if style == "geometric":
        base = Image.new("RGB", size, _rgb(bg_a))
        draw = ImageDraw.Draw(base, "RGBA")
        radius = int(min(width, height) * 0.42)
        draw.ellipse((width - radius, -radius // 2, width + radius, radius * 1.5),
                     fill=_rgb(accent) + (70,))
        draw.ellipse((-radius // 2, int(height * 0.62), radius, int(height * 0.62) + radius),
                     fill=_rgb(bg_b) + (200,))
        draw.polygon([(0, height), (int(width * 0.7), height), (0, int(height * 0.55))],
                     fill=_rgb(accent) + (95,))
        draw.ellipse((int(width * 0.1), int(height * 0.22), int(width * 0.1) + int(width * 0.06),
                      int(height * 0.22) + int(width * 0.06)), outline=_rgb(accent) + (255,), width=6)
        return base

    # gradient
    base = Image.new("RGB", size)
    draw = ImageDraw.Draw(base)
    for y in range(height):
        draw.line((0, y, width, y), fill=_mix(bg_a, bg_b, y / max(1, height - 1)))
    if _blobs_available():
        glow = Image.new("RGBA", size, (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow)
        for index in range(3):
            cx = random.randint(int(width * 0.05), int(width * 0.95))
            cy = random.randint(int(height * 0.05), int(height * 0.95))
            r = random.randint(int(min(width, height) * 0.25), int(min(width, height) * 0.55))
            color = _rgb(accent) if index % 2 == 0 else _rgb(bg_b)
            gdraw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color + (90,))
        glow = glow.filter(ImageFilter.GaussianBlur(radius=int(min(width, height) * 0.09)))
        base = Image.alpha_composite(base.convert("RGBA"), glow).convert("RGB")
        import numpy as np

        noise = np.random.default_rng(seed).normal(0, 4.5, (height, width, 1))
        arr = np.clip(np.asarray(base, dtype=np.float32) + noise, 0, 255).astype("uint8")
        base = Image.fromarray(arr, "RGB")
    return base


def draw_scene(scene: dict, index: int, total: int, out_path: str | Path,
               aspect: str = "9:16 · Shorts/Reels", quality: str = "high",
               style: str = "gradient", watermark: str = "", palette: dict | None = None,
               kicker: str = "") -> Path:
    """Gambar satu frame adegan (PNG) siap dianimasikan FFmpeg."""
    from PIL import Image, ImageDraw, ImageFilter

    width, height = canvas_size(aspect, quality)
    palette = palette or PALETTES[index % len(PALETTES)]
    accent = _hex(scene.get("accent") or palette["accent"], palette["accent"])
    palette = {**palette, "accent": accent}
    portrait = height >= width
    margin = int(width * (0.085 if portrait else 0.07))
    max_width = width - 2 * margin

    image = _background(STYLES.get(style, "gradient"), palette, (width, height), seed=1000 + index)
    draw = ImageDraw.Draw(image)
    editorial = STYLES.get(style) == "editorial"
    base_bg = "#f8fafc" if editorial else palette["bg"][0]
    ink = readable_ink(_rgb(base_bg), "#0b1120" if editorial else palette["ink"])
    if editorial:
        accent = _hex(_mix(accent, "#0b1120", 0.45), accent)

    headline = clean_text(scene.get("headline") or "", 90)
    subheadline = clean_text(scene.get("subheadline") or "", 200)
    bullets = [clean_text(b, 70) for b in (scene.get("bullets") or []) if clean_text(b, 70)][:3]

    kicker_text = clean_text(kicker or f"BAGIAN {index + 1}/{total}", 40).upper()
    title_font, title_lines = fit_text(
        draw, headline, max_width, int(width * (0.105 if portrait else 0.075)),
        min_size=int(width * 0.045), bold=True, serif=STYLES.get(style) == "editorial", max_lines=5)
    line_height = int(title_font.size * 1.18)
    block_height = len(title_lines) * line_height
    sub_font = None
    sub_lines: list[str] = []
    if subheadline:
        sub_font, sub_lines = fit_text(draw, subheadline, max_width,
                                       int(width * (0.048 if portrait else 0.034)),
                                       min_size=int(width * 0.028), max_lines=4)
        block_height += int(sub_font.size * 1.45) * len(sub_lines) + int(height * 0.02)
    bullet_font = None
    if bullets:
        bullet_font = _font(bold=False, serif=False, size=int(width * (0.042 if portrait else 0.03)))
        block_height += len(bullets) * int(bullet_font.size * 1.9)

    kicker_font = _font(bold=True, size=int(width * (0.03 if portrait else 0.022)))
    kicker_h = int(kicker_font.size * 2.3)
    block_height += kicker_h
    top = max(int(height * 0.12), int((height - block_height) / 2 - height * 0.05))
    # Panel transparan supaya teks tetap terbaca di atas latar apa pun.
    panel_top = max(0, top - int(height * 0.028))
    scrim = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scrim)
    panel_color = (255, 255, 255, 120) if STYLES.get(style) == "editorial" else (4, 8, 20, 140)
    panel_bottom = min(height - int(height * 0.09), top + block_height + int(height * 0.04))
    sdraw.rounded_rectangle((int(margin * 0.55), panel_top, width - int(margin * 0.55), panel_bottom),
                            radius=int(width * 0.045), fill=panel_color)
    image = Image.alpha_composite(image.convert("RGBA"), scrim).convert("RGB")
    draw = ImageDraw.Draw(image)

    # label kecil + garis aksen
    draw.text((margin, top), kicker_text, font=kicker_font, fill=accent)
    rule_y = top + int(kicker_font.size * 1.9)
    draw.line((margin, rule_y, margin + int(max_width * 0.22), rule_y), fill=accent,
              width=max(3, int(width * 0.006)))

    y = top + kicker_h
    for line in title_lines:
        draw.text((margin, y), line, font=title_font, fill=ink)
        y += line_height
    if sub_font and sub_lines:
        y += int(height * 0.015)
        for line in sub_lines:
            draw.text((margin, y), line, font=sub_font, fill=ink)
            y += int(sub_font.size * 1.45)
    if bullet_font and bullets:
        y += int(height * 0.015)
        for bullet in bullets:
            radius = max(4, int(bullet_font.size * 0.18))
            draw.ellipse((margin, y + int(bullet_font.size * 0.35), margin + radius * 2,
                          y + int(bullet_font.size * 0.35) + radius * 2), fill=accent)
            draw.text((margin + radius * 2 + int(width * 0.018), y), bullet, font=bullet_font, fill=ink)
            y += int(bullet_font.size * 1.9)

    # indikator progres adegan
    dot_r = max(4, int(width * 0.008))
    gap = dot_r * 4
    total_w = total * gap
    start_x = (width - total_w) // 2 + gap // 2
    dot_y = height - int(height * 0.055)
    for dot in range(total):
        color = accent if dot == index else (255, 255, 255, 90)
        draw.ellipse((start_x + dot * gap - dot_r, dot_y - dot_r,
                      start_x + dot * gap + dot_r, dot_y + dot_r), fill=color)
    if watermark:
        wm_font = _font(bold=False, size=int(width * 0.026))
        text = clean_text(watermark, 40)
        wm_color = "#334155" if STYLES.get(style) == "editorial" else "#cbd5e1"
        draw.text(((width - draw.textlength(text, font=wm_font)) / 2, height - int(height * 0.035)),
                  text, font=wm_font, fill=wm_color)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.filter(ImageFilter.SMOOTH).save(out, "PNG", optimize=True)
    return out


# ───────────────────────── FFmpeg ─────────────────────────
def scene_video_filter(width: int, height: int, duration: float, fps: int = DEFAULT_FPS,
                       motion: bool = True, fade: float = 0.4) -> str:
    """Rantai filter untuk gambar diam: zoom halus + fade masuk/keluar."""
    fade = max(0.0, min(fade, max(0.1, duration / 3)))
    parts = [f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase",
             f"crop={width * 2}:{height * 2}"]
    if motion and duration > 1.2:
        parts.append(
            f"zoompan=z='min(1.0+0.00045*on,1.10)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={width}x{height}:fps={fps}")
    else:
        parts.append(f"scale={width}:{height}")
    if fade > 0 and duration > fade * 2:
        parts.append(f"fade=t=in:st=0:d={fade:.2f}")
        parts.append(f"fade=t=out:st={duration - fade:.2f}:d={fade:.2f}")
    parts.append("format=yuv420p")
    return ",".join(parts)


def build_scene_command(image_path: str | Path, out_path: str | Path, duration: float,
                        width: int, height: int, audio_path: str | Path | None = None,
                        fps: int = DEFAULT_FPS, motion: bool = True,
                        ffmpeg: str | None = None) -> list[str]:
    """Perintah ffmpeg untuk satu adegan (gambar + narasi → klip MP4)."""
    cmd = [ffmpeg or ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
           "-loop", "1", "-t", f"{max(0.5, duration):.3f}", "-i", str(image_path)]
    if audio_path:
        cmd += ["-i", str(audio_path)]
    cmd += ["-vf", scene_video_filter(width, height, max(0.5, duration), fps, motion),
            "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p"]
    if audio_path:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-af", "apad", "-shortest"]
    cmd += ["-movflags", "+faststart", str(out_path)]
    return cmd


def build_srt(scenes: list[dict], durations: list[float],
              offset: float = 0.0) -> str:
    """Subtitle per adegan; kalimat panjang dipecah memakai porsi durasi."""
    def stamp(seconds: float) -> str:
        seconds = max(0.0, seconds)
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        whole = int(secs)
        millis = int(round((secs - whole) * 1000))
        if millis == 1000:
            whole, millis = whole + 1, 0
        return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d},{millis:03d}"

    blocks: list[str] = []
    index = 1
    clock = 0.0
    for scene, duration in zip(scenes, durations):
        duration = max(0.5, float(duration))
        text = clean_text(scene.get("narration") or scene.get("headline") or "", 700)
        chunks: list[str] = []
        for sentence in split_sentences(text) or [text]:
            if len(sentence) <= 60:
                chunks.append(sentence)
                continue
            words, current = sentence.split(), ""
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and len(candidate) > 60:
                    chunks.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                chunks.append(current)
        chunks = [c for c in chunks if c]
        if not chunks:
            clock += duration
            continue
        total_chars = sum(len(c) for c in chunks) or 1
        local = 0.0
        for chunk in chunks:
            span = duration * len(chunk) / total_chars
            start = offset + clock + local
            end = min(offset + clock + duration, start + span)
            blocks.append(f"{index}\n{stamp(start)} --> {stamp(end)}\n{chunk}\n")
            index += 1
            local += span
        clock += duration
    return "\n".join(blocks)


def subtitle_filter(srt_path: str | Path, font_size: int = 12,
                    max_chars_per_line: int = 26, margin_v: int = 40) -> str:
    """Filter libass untuk menempelkan subtitle dengan gaya studio."""
    style = (f"FontName=DejaVu Sans,Fontsize={int(font_size)},PrimaryColour=&H00FFFFFF,"
             f"OutlineColour=&H90000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,"
             f"MarginV={int(margin_v)},Bold=0,MaxCharsPerLine={int(max_chars_per_line)}")
    folder = fonts_dir()
    fontsdir = f"fontsdir='{folder}':" if folder else ""
    return f"subtitles={Path(srt_path).name}:{fontsdir}force_style='{style}'"


def burn_subtitles(input_path: str | Path, srt_path: str | Path, out_path: str | Path,
                   workdir: Path | None = None, font_size: int = 12,
                   max_chars_per_line: int = 26, ffmpeg: str | None = None) -> Path:
    """Tempelkan subtitle (libass). Melempar RelayVideoError bila libass tidak ada."""
    workdir = Path(workdir or Path(srt_path).parent)
    target = workdir / "subs-burn.mp4"
    cmd = [ffmpeg or ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(Path(input_path).resolve()),
           "-vf", subtitle_filter(srt_path, font_size=font_size,
                                  max_chars_per_line=max_chars_per_line),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
           "-c:a", "copy", "-movflags", "+faststart", str(target)]
    res = _run(cmd, cwd=workdir)
    if res.returncode != 0 or not target.is_file():
        raise RelayVideoError(f"Subtitle gagal ditempel: {(res.stderr or '')[-400:]}")
    shutil.move(str(target), str(out_path))
    return Path(out_path)


def mix_music(video_path: str | Path, music_path: str | Path, out_path: str | Path,
              volume: float = 0.18, ffmpeg: str | None = None) -> Path:
    """Campur musik latar di bawah narasi (musik diulang sampai video selesai).

    Bila video belum punya track audio (mis. tanpa narasi), musik langsung menjadi
    audio utama video.
    """
    gain = max(0.0, min(1.5, volume))
    base = [ffmpeg or ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path), "-stream_loop", "-1", "-i", str(music_path)]
    if has_audio_stream(video_path):
        graph = (f"[1:a]volume={gain:.2f},afade=t=in:st=0:d=1.2[bg];"
                 "[bg][0:a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
        cmd = base + ["-filter_complex", graph, "-map", "0:v", "-map", "[aout]"]
    else:
        graph = f"[1:a]volume={gain:.2f},afade=t=in:st=0:d=1.2[aout]"
        cmd = base + ["-filter_complex", graph, "-map", "0:v", "-map", "[aout]"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-shortest", "-movflags", "+faststart", str(out_path)]
    res = _run(cmd)
    if res.returncode != 0 or not Path(out_path).is_file():
        raise RelayVideoError(f"Musik latar gagal dicampur: {(res.stderr or '')[-400:]}")
    return Path(out_path)


def concat_clips(clips: list[Path], out_path: Path, workdir: Path,
                 fps: int = DEFAULT_FPS) -> Path:
    """Gabung klip adegan; utamakan concat demuxer (tanpa re-encode)."""
    if not clips:
        raise RelayVideoError("Tidak ada klip adegan untuk digabung.")
    if len(clips) == 1:
        shutil.copyfile(clips[0], out_path)
        return out_path
    listing = workdir / "clips.txt"
    listing.write_text("".join(f"file '{c.resolve().as_posix()}'\n" for c in clips), "utf-8")
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(listing),
           "-c", "copy", "-movflags", "+faststart", str(out_path)]
    res = _run(cmd)
    if res.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 1024:
        return out_path
    # Fallback: re-encode dengan filter concat (aman untuk klip berbeda parameter).
    inputs: list[str] = []
    for clip in clips:
        inputs += ["-i", str(clip)]
    graph = "".join(f"[{i}:v][{i}:a]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=1[v][a]"
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", *inputs,
           "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
           "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", str(out_path)]
    res = _run(cmd)
    if res.returncode != 0 or not out_path.is_file():
        raise RelayVideoError(f"Gagal menggabungkan adegan: {(res.stderr or '')[-400:]}")
    return out_path


# ───────────────────────── pipeline ─────────────────────────
def scene_durations(scenes: list[dict], narrations: list[Path | None],
                    seconds_per_scene: float = 6.0) -> list[float]:
    durations: list[float] = []
    for scene, audio in zip(scenes, narrations):
        if audio:
            measured = media_duration(audio)
            duration = measured + SCENE_PAD if measured > 0.1 else (seconds_per_scene + SCENE_PAD)
        else:
            words = len(str(scene.get("narration") or scene.get("headline") or "").split())
            duration = max(seconds_per_scene, min(MAX_SCENE_SECONDS, 2.0 + words * 0.42))
        durations.append(round(max(MIN_SCENE_SECONDS, min(MAX_SCENE_SECONDS, duration)), 3))
    return durations


def render_text_video(job: dict | None, plan: dict, output_path: str | Path,
                      aspect: str = "9:16 · Shorts/Reels", quality: str = "high",
                      style: str = "gradient", voice: str = DEFAULT_VOICE,
                      rate: str = "+0%", narrate: bool = True,
                      subtitles: bool = True, music_path: str | Path | None = None,
                      music_volume: float = 0.18, watermark: str = "",
                      kicker_prefix: str = "BAGIAN",
                      seconds_per_scene: float = 6.0) -> tuple[bool, str]:
    """Render skenario menjadi MP4. Mengembalikan (berhasil, pesan)."""
    plan = normalize_plan(plan)
    scenes = plan["scenes"]
    if not scenes:
        raise RelayVideoError("Skenario tidak memiliki adegan.")
    width, height = canvas_size(aspect, quality)
    fps = DEFAULT_FPS
    out = Path(output_path)
    if out.suffix.lower() != ".mp4":
        out = out.with_suffix(".mp4")
    out.parent.mkdir(parents=True, exist_ok=True)

    workdir = TMP_DIR / f"t2v-{uuid.uuid4().hex[:10]}"
    workdir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        narrations: list[Path | None] = [None] * len(scenes)
        tts_warning = ""
        if narrate:
            _note(job, f"Membuat narasi TTS ({voice}) 0/{len(scenes)}…")
            for index, scene in enumerate(scenes):
                text = scene.get("narration") or scene.get("headline")
                audio_path = workdir / f"voice-{index:02d}.mp3"
                try:
                    narrations[index] = synthesize_speech(text, audio_path, voice=voice, rate=rate)
                except RelayVideoError as exc:
                    # TTS gagal (mis. server tanpa akses ke layanan suara): video tetap
                    # dirender hening, subtitle tetap tampil dari naskah.
                    narrations[index] = None
                    if not tts_warning:
                        tts_warning = str(exc)
                _note(job, f"Narasi {index + 1}/{len(scenes)} siap."
                      if narrations[index] else f"Narasi {index + 1} dilewati.")
            if tts_warning:
                _note(job, f"Tanpa suara: {tts_warning}")

        durations = scene_durations(scenes, narrations, seconds_per_scene)
        images: list[Path] = []
        _note(job, "Menggambar visual adegan…")
        for index, scene in enumerate(scenes):
            images.append(draw_scene(
                scene, index, len(scenes), workdir / f"scene-{index:02d}.png",
                aspect=aspect, quality=quality, style=style, watermark=watermark,
                kicker=f"{kicker_prefix} {index + 1}/{len(scenes)}"))

        clips: list[Path] = []
        for index, (image, duration) in enumerate(zip(images, durations)):
            _note(job, f"Merender adegan {index + 1}/{len(scenes)} ({duration:.1f}s)…")
            clip = workdir / f"clip-{index:02d}.mp4"
            cmd = build_scene_command(image, clip, duration, width, height,
                                      audio_path=narrations[index], fps=fps)
            res = _run(cmd)
            if res.returncode != 0 or not clip.is_file():
                # Coba sekali lagi tanpa zoompan (ffmpeg lama / build tanpa filter).
                res = _run(build_scene_command(image, clip, duration, width, height,
                                               audio_path=narrations[index], fps=fps, motion=False))
            if res.returncode != 0 or not clip.is_file():
                raise RelayVideoError(f"Adegan {index + 1} gagal dirender: {(res.stderr or '')[-400:]}")
            clips.append(clip)

        _note(job, "Menggabungkan adegan…")
        merged = concat_clips(clips, workdir / "merged.mp4", workdir, fps=fps)

        current = merged
        has_narration = any(narrations)
        if subtitles and has_narration:
            srt = workdir / "narasi.srt"
            srt.write_text(build_srt(scenes, durations), "utf-8")
            _note(job, "Menempelkan subtitle…")
            try:
                current = burn_subtitles(current, srt, workdir / "with-subs.mp4", workdir=workdir)
            except RelayVideoError as exc:
                _note(job, f"Subtitle dilewati ({exc})")

        if music_path:
            _note(job, "Mencampur musik latar…")
            current = mix_music(current, music_path, workdir / "with-music.mp4", volume=music_volume)

        _note(job, "Menyimpan hasil…")
        shutil.move(str(current), str(out))
        plan_file = PLAN_DIR / f"{out.stem}.json"
        PLAN_DIR.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(json.dumps({
            "title": plan.get("title"), "voice": voice, "aspect": aspect, "quality": quality,
            "style": style, "watermark": watermark, "music": str(music_path or ""),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "scenes": scenes,
        }, ensure_ascii=False, indent=2), "utf-8")
        size_mb = out.stat().st_size / (1024 * 1024)
        duration = media_duration(out)
        info = (f"{out.name} · {duration:.1f}s · {size_mb:.1f} MB · "
                f"{len(scenes)} adegan · {time.time() - started:.0f}s render")
        if narrate and not has_narration:
            info += f" · tanpa narasi ({tts_warning[:160]})"
        return True, info
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def render_plan_file(plan_path: str | Path, output_path: str | Path, **kwargs) -> tuple[bool, str]:
    plan = json.loads(Path(plan_path).read_text("utf-8"))
    return render_text_video(None, plan, output_path, **kwargs)


def submit_text_video(jobs, plan: dict, output_path: str | Path, *, title: str | None = None,
                      **kwargs) -> dict:
    """Masukkan render video ke antrean JobRunner aplikasi."""
    from functools import partial

    name = Path(output_path).name or "video.mp4"
    return jobs.submit(title or f"T2V · {name}",
                       partial(render_text_video, plan=plan, output_path=output_path, **kwargs))
