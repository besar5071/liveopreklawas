"""Kie.ai music-generation API helpers used by the Streamlit Music Studio."""
from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests


API_BASE = "https://api.kie.ai/api/v1/jobs"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{3,150}$")


class KieMusicError(RuntimeError):
    """Sanitized provider error; ``uncertain`` means a create request may have landed."""

    def __init__(self, message: str, status: int = 0, uncertain: bool = False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


def _safe_text(value: Any, limit: int = 350, secret: str = "") -> str:
    text = str(value or "").strip()
    if secret:
        text = text.replace(secret, "[redacted]")
    return text[:limit]


def _https_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return ""
    return value.strip()


def _api_request(api_key: str, method: str, path: str, *, body: dict | None = None,
                 params: dict | None = None, timeout: int = 30, create: bool = False) -> dict:
    key = str(api_key or "").strip()
    if not key:
        raise KieMusicError("API key Kie.ai belum diisi.")
    try:
        response = requests.request(
            method, API_BASE + path,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            json=body, params=params, timeout=timeout,
        )
    except requests.RequestException as exc:
        raise KieMusicError(
            "Koneksi ke Kie.ai terputus atau melewati batas waktu. "
            + ("Status pengiriman belum pasti; cek Task ID/akun sebelum mencoba ulang." if create else "Coba cek status lagi."),
            uncertain=create,
        ) from exc
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        uncertain = create and (response.ok or response.status_code >= 500)
        raise KieMusicError(
            "Respons Kie.ai tidak dapat dibaca." + (" Status pengiriman belum pasti." if uncertain else ""),
            response.status_code, uncertain,
        ) from exc
    if not isinstance(payload, dict):
        raise KieMusicError("Format respons Kie.ai tidak dikenali.", response.status_code,
                            create and (response.ok or response.status_code >= 500))
    raw_code = payload.get("code")
    try:
        code = int(raw_code) if raw_code is not None else 200
    except (TypeError, ValueError):
        code = 200
    if not response.ok or code not in (0, 200):
        status = code if code not in (0, 200) else response.status_code
        message = _safe_text(payload.get("msg") or payload.get("message") or payload.get("error"), secret=key)
        if status in (401, 403):
            message = "API key Kie.ai tidak valid atau tidak memiliki akses."
        elif status in (402, 455):
            message = "Kredit atau akses model Kie.ai tidak mencukupi."
        elif not message:
            message = f"Kie.ai menolak permintaan (HTTP {status})."
        raise KieMusicError(message, status, create and (status == 408 or status >= 500))
    return payload


def create_task(api_key: str, body: dict) -> tuple[str, dict]:
    """Create one music task; returns its validated task ID and raw response."""
    payload = _api_request(api_key, "POST", "/createTask", body=body, timeout=45, create=True)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    task_id = data.get("taskId") or data.get("task_id")
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise KieMusicError("Kie.ai tidak mengembalikan Task ID yang valid. Periksa akun sebelum mengirim ulang.",
                            uncertain=True)
    return task_id, payload


def get_task(api_key: str, task_id: str) -> dict:
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise KieMusicError("Task ID tidak valid.")
    return _api_request(api_key, "GET", "/recordInfo", params={"taskId": task_id}, timeout=25)


def _public_download_url(value: str) -> str:
    url = _https_url(value)
    if not url:
        raise KieMusicError("URL audio bukan HTTPS yang valid.")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".test")):
        raise KieMusicError("Host URL audio tidak diizinkan.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise KieMusicError("Port URL audio tidak valid.") from exc
    if port not in (None, 443):
        raise KieMusicError("Port URL audio tidak diizinkan.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise KieMusicError("URL audio mengarah ke alamat jaringan privat.")
    return url


def download_audio(url: str, max_bytes: int = 150 * 1024 * 1024) -> tuple[bytes, str, str]:
    """Fetch one generated audio file safely; returns bytes, MIME type, and extension."""
    current_url = _public_download_url(url)
    response = None
    for redirect_count in range(6):
        try:
            response = requests.get(current_url, timeout=(10, 60), stream=True, allow_redirects=False)
        except requests.RequestException as exc:
            raise KieMusicError("Gagal mengunduh audio dari penyedia. Coba lagi.") from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            response = None
            if not location or redirect_count >= 5:
                raise KieMusicError("Tautan unduhan terlalu banyak mengalihkan atau tidak valid.")
            current_url = _public_download_url(urljoin(current_url, location))
            continue
        break
    try:
        if response is None or not response.ok:
            status = response.status_code if response is not None else 0
            raise KieMusicError(f"Penyedia menolak unduhan audio (HTTP {status}).", status)
        content_type = str(response.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].strip().lower()
        if content_type == "text/html" or not (content_type.startswith("audio/") or content_type == "application/octet-stream"):
            raise KieMusicError("Tautan tidak mengembalikan file audio yang dapat diunduh.")
        try:
            announced_size = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            announced_size = 0
        if announced_size > max_bytes:
            raise KieMusicError("File audio melebihi batas unduh 150 MB.")
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise KieMusicError("File audio melebihi batas unduh 150 MB.")
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise KieMusicError("File audio kosong.")
        extension_by_type = {
            "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/aac": "aac",
            "audio/wav": "wav", "audio/x-wav": "wav", "audio/ogg": "ogg", "audio/flac": "flac",
            "audio/x-flac": "flac", "audio/opus": "opus", "audio/aiff": "aiff", "audio/x-aiff": "aiff",
        }
        extension = extension_by_type.get(content_type, "")
        if not extension:
            path_suffix = Path(urlparse(current_url).path).suffix.lower().lstrip(".")
            extension = path_suffix if path_suffix in {"mp3", "m4a", "aac", "wav", "ogg", "flac", "opus", "aiff"} else "mp3"
        return data, content_type, extension
    except requests.RequestException as exc:
        raise KieMusicError("Unduhan audio terputus. Coba lagi.") from exc
    finally:
        if response is not None:
            response.close()


def _clean_track(item: dict) -> dict | None:
    audio = _https_url(item.get("audio") or item.get("audio_url") or item.get("audioUrl")
                       or item.get("source_audio_url") or item.get("sourceAudioUrl") or item.get("url"))
    stream = _https_url(item.get("stream") or item.get("stream_audio_url") or item.get("streamAudioUrl")
                        or item.get("stream_url") or item.get("streamUrl"))
    if not audio and not stream:
        return None
    try:
        duration = float(item.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "id": _safe_text(item.get("id") or item.get("audioId") or item.get("audio_id"), 150),
        "audio": audio,
        "stream": stream,
        "image": _https_url(item.get("image") or item.get("image_url") or item.get("imageUrl")
                             or item.get("source_image_url") or item.get("sourceImageUrl")),
        "title": _safe_text(item.get("title") or item.get("name"), 200),
        "duration": duration if duration > 0 else 0.0,
    }


def extract_tracks(payload: Any) -> list[dict]:
    """Find safe, de-duplicated audio URLs in the several Kie.ai result envelopes."""
    queue: list[Any] = [payload.get("data", payload) if isinstance(payload, dict) else payload]
    visited: set[int] = set()
    found: list[dict] = []
    steps = 0
    nested_keys = ("response", "resultJson", "result_json", "sunoData", "suno_data", "tracks", "songs",
                   "clips", "data", "result", "output")
    while queue and steps < 300:
        part = queue.pop(0)
        steps += 1
        if part is None:
            continue
        if isinstance(part, str):
            text = part.strip()
            if len(text) < 2_000_000 and text[:1] in ("{", "["):
                try:
                    queue.append(json.loads(text))
                except (ValueError, TypeError):
                    pass
            continue
        if not isinstance(part, (dict, list)) or id(part) in visited:
            continue
        visited.add(id(part))
        if isinstance(part, list):
            queue.extend(part[:40])
            continue
        track = _clean_track(part)
        if track:
            found.append(track)
        urls = part.get("resultUrls")
        if isinstance(urls, list):
            for url in urls[:20]:
                safe = _https_url(url)
                if safe:
                    found.append({"id": "", "audio": safe, "stream": "", "image": "", "title": "", "duration": 0.0})
        for key in nested_keys:
            child = part.get(key)
            if child is not None:
                queue.append(child)
    result: list[dict] = []
    by_key: dict[str, dict] = {}
    for track in found:
        key = "id:" + track["id"] if track["id"] else "url:" + (track["audio"] or track["stream"])
        previous = by_key.get(key)
        if previous is None:
            previous = dict(track)
            by_key[key] = previous
            result.append(previous)
        else:
            for field in ("audio", "stream", "image", "title", "duration"):
                if not previous.get(field) and track.get(field):
                    previous[field] = track[field]
    return result[:20]


def normalize_task(payload: dict, api_key: str = "") -> dict:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw_status = str(data.get("state") or data.get("status") or data.get("taskStatus") or "").strip().lower()
    raw_status = re.sub(r"[\s-]+", "_", raw_status)
    tracks = extract_tracks(payload)
    if raw_status in {"success", "succeeded", "complete", "completed"}:
        status = "success" if tracks else "waiting"
        message = "Musik selesai dibuat." if tracks else "Proses selesai, tetapi tautan audio belum tersedia."
    elif raw_status in {"fail", "failed", "error", "cancelled", "canceled"}:
        status = "failed"
        message = _safe_text(data.get("failMsg") or data.get("errorMessage") or data.get("error_message") or data.get("message"), secret=api_key)
        message = "Tugas dinyatakan gagal oleh Kie.ai." + (f" {message}" if message else "")
    elif raw_status in {"generating", "processing", "in_progress", "first_success", "text_success", "running"}:
        status, message = "generating", "Kie.ai sedang membuat musik."
    else:
        status, message = "waiting", "Tugas berada dalam antrean Kie.ai."
    try:
        progress = float(data.get("progress"))
        if progress < 0 or progress > 100:
            progress = None
    except (TypeError, ValueError):
        progress = None
    return {"status": status, "message": message, "progress": progress, "tracks": tracks}


def validate_request(body: dict) -> dict:
    """Validate the music API payload locally, before credit-bearing requests."""
    if not isinstance(body, dict) or body.get("model") != "ai-music-api/generate":
        raise ValueError("Model generator musik tidak valid.")
    data = body.get("input")
    if not isinstance(data, dict):
        raise ValueError("Input musik tidak valid.")
    title = str(data.get("title") or "").strip()
    style = str(data.get("style") or "").strip()
    lyrics = str(data.get("lyrics") or "").strip()
    instrumental = bool(data.get("instrumental"))
    if not title or len(title) > 80:
        raise ValueError("Judul lagu wajib diisi dan maksimal 80 karakter.")
    if not style or len(style) > 1000:
        raise ValueError("Style musik wajib diisi dan maksimal 1000 karakter.")
    if not instrumental and (not lyrics or len(lyrics) > 5000):
        raise ValueError("Lirik wajib diisi untuk lagu vokal dan maksimal 5000 karakter.")
    model = data.get("model")
    if model not in {"V6", "V6_MINI", "V6_WILD"}:
        raise ValueError("Pilih model musik yang didukung.")
    if len(str(data.get("negative_tags") or "")) > 1000:
        raise ValueError("Daftar gaya/suara yang dihindari maksimal 1.000 karakter.")
    if "duration" in data and (not isinstance(data["duration"], int) or not 10 <= data["duration"] <= 360):
        raise ValueError("Durasi target harus bilangan bulat 10–360 detik.")
    if "variety" in data and (not isinstance(data["variety"], int) or not 0 <= data["variety"] <= 4):
        raise ValueError("Variasi harus bilangan bulat 0–4.")
    for key in ("style_weight", "weirdness_constraint"):
        if key in data and (not isinstance(data[key], (int, float)) or not 0 <= data[key] <= 1):
            raise ValueError(f"{key} harus berada pada rentang 0–1.")
    safe = dict(data)
    safe.update({"title": title, "style": style, "instrumental": instrumental})
    if instrumental:
        safe.pop("lyrics", None)
    return {"model": "ai-music-api/generate", "input": safe}
