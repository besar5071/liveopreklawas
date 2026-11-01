"""Ollama Cloud helpers for Indonesian Shorts scripts and YouTube metadata."""
from __future__ import annotations

import json
import re

import requests


class OllamaCloudError(RuntimeError):
    pass


def _youtube_references(access_token: str, query: str) -> list[dict]:
    if not access_token:
        return []
    try:
        r = requests.get("https://www.googleapis.com/youtube/v3/search", params={
            "part": "snippet", "type": "video", "videoDuration": "short", "order": "viewCount",
            "maxResults": 5, "q": query,
        }, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
        r.raise_for_status()
        return [{"title": x.get("snippet", {}).get("title", ""),
                 "video_id": x.get("id", {}).get("videoId", ""), "views_label": "referensi populer"}
                for x in r.json().get("items", [])]
    except requests.RequestException:
        return []


def _chat(api_key: str, model: str, system: str, prompt: str) -> str:
    if not api_key.strip():
        raise OllamaCloudError("API key Ollama Cloud belum diisi.")
    try:
        r = requests.post("https://ollama.com/v1/chat/completions", headers={
            "Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json",
        }, json={"model": model or "gpt-oss:20b", "temperature": 0.8,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}, timeout=120)
        if not r.ok:
            raise OllamaCloudError(f"Ollama Cloud ({r.status_code}): {r.text[:500]}")
        return str(r.json()["choices"][0]["message"]["content"])
    except requests.RequestException as exc:
        raise OllamaCloudError(f"Tidak dapat menghubungi Ollama Cloud: {exc}") from exc


def _json_from_text(text: str) -> dict:
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise OllamaCloudError("Respons Ollama tidak berisi JSON yang valid.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise OllamaCloudError("Respons JSON Ollama tidak bisa dibaca.") from exc


def generate_viral_short_script(api_key: str, topic_key: str = "relationship", custom_topic: str = "",
                                watermark: str = "", model: str = "gpt-oss:20b", youtube_access_token: str = "") -> dict:
    topic = custom_topic or topic_key
    refs = _youtube_references(youtube_access_token, topic)
    prompt = f"""Buat konsep YouTube Shorts berbahasa Indonesia tentang: {topic}.
Buat naskah orisinal, emosional tetapi tidak menyesatkan, durasi baca 20-35 detik.
Balas JSON saja dengan key: header (maks 7 kata), body (bait dipisahkan newline kosong),
clipdrop_prompt (prompt gambar latar sinematik bahasa Inggris, tanpa teks), youtube_title (maks 100 karakter),
youtube_description, youtube_tags (array 6-12 tag), trending_keywords (array), topic_label.
Watermark opsional: {watermark or 'tidak ada'}.
Referensi judul populer untuk inspirasi umum (jangan salin): {json.dumps([x['title'] for x in refs], ensure_ascii=False)}"""
    data = _json_from_text(_chat(api_key, model,
        "Kamu penulis naskah video vertikal Shorts dan editor SEO YouTube Indonesia. Jangan membuat klaim fakta tanpa sumber.", prompt))
    data["youtube_references"] = refs
    return data


def generate_viral_short_upload_metadata(api_key: str, header: str, body: str, topic_key: str = "relationship",
                                         custom_topic: str = "", watermark: str = "", model: str = "gpt-oss:20b",
                                         youtube_access_token: str = "") -> dict:
    topic = custom_topic or topic_key
    refs = _youtube_references(youtube_access_token, topic)
    prompt = f"""Optimalkan metadata untuk YouTube Shorts berbahasa Indonesia berdasarkan materi berikut.
Tema: {topic}\nJudul visual: {header}\nIsi: {body}\nWatermark: {watermark}
Referensi tren (hanya inspirasi, jangan salin): {json.dumps([x['title'] for x in refs], ensure_ascii=False)}
Balas JSON saja: youtube_title (maks 100 karakter dan memuat #Shorts), youtube_description (CTA natural + 3-6 hashtag relevan), youtube_tags (array 6-15 kata kunci), trending_keywords (array), topic_label."""
    data = _json_from_text(_chat(api_key, model,
        "Kamu ahli metadata YouTube Shorts Indonesia. Tulis spesifik dan alami, hindari spam dan janji viral.", prompt))
    data["youtube_title"] = str(data.get("youtube_title", "#Shorts"))[:100]
    data["youtube_tags"] = [str(x) for x in data.get("youtube_tags", []) if str(x).strip()][:15]
    data["youtube_references"] = refs
    return data
