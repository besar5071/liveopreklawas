"""Minimal OAuth 2.0 helpers for YouTube Shorts uploads.

Tokens are stored under data/ (never in source control). OAuth client secrets should
be supplied through Streamlit secrets, not committed to the repository.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API_ROOT = "https://www.googleapis.com/youtube/v3"
UPLOAD_ROOT = "https://www.googleapis.com/upload/youtube/v3"
ACCOUNT_FILE = Path(__file__).resolve().parent / "data" / "youtube_accounts.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


class YouTubeAuthError(RuntimeError):
    pass


def make_state(secret: str) -> str:
    nonce = secrets.token_urlsafe(24)
    signature = hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return f"{nonce}.{signature}"


def verify_state(state: str, secret: str) -> bool:
    try:
        nonce, supplied = state.rsplit(".", 1)
        expected = hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(supplied, expected)
    except (ValueError, AttributeError):
        return False


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "state": state,
    })


def parse_redirect_url(url: str, expected_state: str, state_secret: str) -> str:
    try:
        q = parse_qs(urlparse(url.strip()).query)
        state = (q.get("state") or [""])[0]
        code = (q.get("code") or [""])[0]
    except Exception as exc:  # noqa: BLE001
        raise YouTubeAuthError("URL redirect tidak valid.") from exc
    if not code or not state or not hmac.compare_digest(state, expected_state) or not verify_state(state, state_secret):
        raise YouTubeAuthError("Kode OAuth tidak ada atau state tidak cocok. Mulai login lagi.")
    return code


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict:
    r = requests.post(TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri,
        "grant_type": "authorization_code", "code": code,
    }, timeout=30)
    if not r.ok:
        raise YouTubeAuthError(f"Gagal menukar kode OAuth ({r.status_code}): {r.text[:500]}")
    token = r.json()
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
    return token


def _load_accounts() -> list[dict]:
    try:
        data = json.loads(ACCOUNT_FILE.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def saved_accounts() -> list[dict]:
    return [{"id": a["id"], "channel": a.get("channel", {})} for a in _load_accounts()]


def upsert_saved_account(token: dict, channel: dict) -> list[dict]:
    channel_id = channel.get("id")
    if not channel_id:
        raise YouTubeAuthError("Channel YouTube tidak memiliki ID.")
    accounts = _load_accounts()
    rec = next((a for a in accounts if a.get("id") == channel_id), {})
    rec.update({"id": channel_id, "channel": channel, "refresh_token": token.get("refresh_token") or rec.get("refresh_token"),
                "access_token": token.get("access_token"), "expires_at": token.get("expires_at", 0)})
    accounts = [a for a in accounts if a.get("id") != channel_id] + [rec]
    ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = ACCOUNT_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), "utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    temp.replace(ACCOUNT_FILE)
    return saved_accounts()


def _refresh(account: dict, cfg: dict) -> dict:
    refresh_token = account.get("refresh_token")
    if not refresh_token:
        raise YouTubeAuthError("Refresh token tidak tersedia. Hapus dan hubungkan ulang channel.")
    r = requests.post(TOKEN_URL, data={
        "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    }, timeout=30)
    if not r.ok:
        raise YouTubeAuthError(f"Gagal memperbarui token YouTube ({r.status_code}): {r.text[:400]}")
    tok = r.json()
    account.update({"access_token": tok["access_token"], "expires_at": time.time() + int(tok.get("expires_in", 3600))})
    all_accounts = _load_accounts()
    for i, a in enumerate(all_accounts):
        if a.get("id") == account["id"]:
            all_accounts[i] = account
            break
    ACCOUNT_FILE.write_text(json.dumps(all_accounts, ensure_ascii=False, indent=2), "utf-8")
    return tok


def account_access_token(cfg: dict, account_id: str | None = None) -> tuple[str, dict]:
    accounts = _load_accounts()
    account = next((a for a in accounts if a.get("id") == account_id), None) if account_id else (accounts[0] if accounts else None)
    if not account:
        raise YouTubeAuthError("Belum ada channel YouTube yang terhubung.")
    if float(account.get("expires_at", 0)) < time.time() + 60:
        tok = _refresh(account, cfg)
        return tok["access_token"], account.get("channel", {})
    return account.get("access_token", ""), account.get("channel", {})


def get_my_channel(access_token: str) -> dict:
    r = requests.get(API_ROOT + "/channels", params={"part": "snippet", "mine": "true"},
                     headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if not r.ok:
        raise YouTubeAuthError(f"Tidak bisa membaca channel YouTube ({r.status_code}): {r.text[:400]}")
    items = r.json().get("items", [])
    if not items:
        raise YouTubeAuthError("Akun Google ini tidak memiliki channel YouTube yang dapat diakses.")
    item = items[0]
    return {"id": item["id"], "title": item.get("snippet", {}).get("title", "YouTube")}


def upload_video(access_token: str, video_path: Path, title: str, description: str, tags, privacy: str = "public") -> dict:
    """Upload one MP4 using YouTube's resumable upload protocol."""
    video_path = Path(video_path)
    if not video_path.is_file():
        raise FileNotFoundError(video_path)
    clean_tags: list[str] = []
    tag_chars = 0
    for raw_tag in tags:
        tag = str(raw_tag).strip()
        if tag and tag_chars + len(tag) <= 500:
            clean_tags.append(tag)
            tag_chars += len(tag)
    metadata = {"snippet": {"title": title[:100], "description": description[:5000],
                            "tags": clean_tags, "categoryId": "22"},
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8",
               "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(video_path.stat().st_size)}
    r = requests.post(UPLOAD_ROOT + "/videos", params={"uploadType": "resumable", "part": "snippet,status"},
                      headers=headers, json=metadata, timeout=45)
    if not r.ok or not r.headers.get("Location"):
        raise YouTubeAuthError(f"YouTube menolak sesi upload ({r.status_code}): {r.text[:500]}")
    session_url = r.headers["Location"]
    total = video_path.stat().st_size
    chunk_size = 8 * 1024 * 1024  # kelipatan 256 KiB sesuai protokol resumable Google
    item = None
    with video_path.open("rb") as f:
        offset = 0
        while offset < total:
            chunk = f.read(min(chunk_size, total - offset))
            end = offset + len(chunk) - 1
            r = requests.put(session_url, data=chunk, headers={
                "Authorization": f"Bearer {access_token}", "Content-Type": "video/mp4",
                "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{total}",
            }, timeout=(30, 300))
            if r.status_code in (200, 201):
                item = r.json()
                offset = total
                break
            if r.status_code != 308:
                raise YouTubeAuthError(f"Upload YouTube gagal ({r.status_code}): {r.text[:500]}")
            # Honor server confirmation to avoid skipping bytes if a chunk was only partly accepted.
            accepted = r.headers.get("Range", "")
            match = re.search(r"bytes=0-(\d+)", accepted)
            new_offset = int(match.group(1)) + 1 if match else 0
            if new_offset <= offset:
                raise YouTubeAuthError("YouTube belum menerima data upload; coba ulang upload.")
            offset = new_offset
            f.seek(offset)
    if item is None:
        raise YouTubeAuthError("Sesi upload berakhir tanpa respons final dari YouTube.")
    video_id = item.get("id")
    if not video_id:
        raise YouTubeAuthError("YouTube tidak mengembalikan ID video setelah upload.")
    return {"video_id": video_id, "shorts_url": f"https://youtube.com/shorts/{video_id}",
            "studio_url": f"https://studio.youtube.com/video/{video_id}/edit"}
