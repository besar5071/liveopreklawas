"""Ollama Cloud helpers for Indonesian Shorts scripts and YouTube metadata."""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

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
    if not isinstance(text, str) or not text.strip():
        raise OllamaCloudError("Ollama mengembalikan jawaban kosong, bukan JSON.")
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    decoder = json.JSONDecoder()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Accept a JSON object surrounded by prose or Markdown, without mistaking braces in prose.
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            data, _ = decoder.raw_decode(cleaned[index:])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    raise OllamaCloudError("Respons Ollama tidak berisi objek JSON yang valid.")


def _web_research(api_key: str, topic: str) -> dict:
    query = f"YouTube Indonesia {topic} live audio musik {time.strftime('%Y')}"[:500]
    try:
        response = requests.post(
            "https://ollama.com/api/web_search", json={"query": query, "max_results": 6},
            headers={"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"},
            timeout=(15, 60),
        )
        if not response.ok:
            return {"query": query, "references": [], "warning": f"Ollama web search tidak tersedia (HTTP {response.status_code})."}
        payload = response.json()
        refs = []
        for item in payload.get("results", [])[:6]:
            url = str(item.get("url") or "")
            if not url.startswith(("https://", "http://")):
                continue
            refs.append({"title": str(item.get("title") or "")[:200], "url": url,
                         "snippet": str(item.get("content") or item.get("snippet") or "")[:700],
                         "source": "Ollama Cloud web search"})
        return {"query": query, "references": refs}
    except (requests.RequestException, ValueError) as exc:
        return {"query": query, "references": [], "warning": f"Riset web gagal; metadata hanya memakai analisis audio ({type(exc).__name__})."}


LIVE_METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 8},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "category_id": {"type": "string"}, "reason": {"type": "string"},
    },
    "required": ["title", "description", "hashtags", "tags", "category_id", "reason"],
}


def _chat_live_metadata(api_key: str, model: str, topic: str, audio_analysis: dict,
                        research: dict, youtube_refs: list[dict], spectrogram_paths: list[str | Path]) -> dict:
    if not api_key.strip():
        raise OllamaCloudError("API key Ollama Cloud belum diisi.")
    evidence = {"topic": topic, "live": True, "audio_analysis": audio_analysis,
                "web_research": research, "youtube_references": youtube_refs}
    system = ("Buat metadata YouTube Live berbahasa Indonesia yang menarik, jujur, dan mudah ditemukan. "
              "Gunakan bukti PCM/spektrogram dan hasil riset yang diberikan; fitur spektral dan tempo hanya heuristik, "
              "bukan identifikasi lagu, artis, lirik, atau genre yang pasti. Jangan mengarang identitas, view count, "
              "tren, atau klaim yang tidak ada dalam riset. Judul <=90 karakter sebelum hashtag; description <=4200 karakter. "
              "Sertakan 1-2 hashtag relevan di judul serta 3-6 hashtag relevan di description. Hashtag bukan jaminan viral. "
              "Keluarkan satu objek JSON saja dengan keys: title, description, hashtags, tags, category_id, reason; tanpa Markdown atau teks lain.")
    user_message = {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}
    image_data = []
    for path in spectrogram_paths[:3]:
        try:
            image_data.append(base64.b64encode(Path(path).read_bytes()).decode("ascii"))
        except OSError:
            continue
    if image_data:
        user_message["images"] = image_data
    payload = {"model": model or "gemma4:31b", "stream": False, "think": False,
               "format": LIVE_METADATA_SCHEMA,
               "options": {"temperature": 0.25, "num_predict": 1200},
               "messages": [{"role": "system", "content": system}, user_message]}

    def request_content(request_payload: dict) -> str:
        try:
            response = requests.post("https://ollama.com/api/chat", json=request_payload,
                                     headers={"Authorization": f"Bearer {api_key.strip()}",
                                              "Content-Type": "application/json"}, timeout=(20, 180))
            if not response.ok:
                raise OllamaCloudError(f"Ollama Cloud metadata gagal ({response.status_code}): {response.text[:500]}. "
                                       "Pastikan [ollama] live_model mendukung gambar/vision dan JSON terstruktur.")
            result = response.json()
        except requests.RequestException as exc:
            raise OllamaCloudError(f"Tidak dapat menghubungi Ollama Cloud untuk metadata: {exc}") from exc
        message = result.get("message", {}) if isinstance(result, dict) else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
        return str(content or result.get("response", "") if isinstance(result, dict) else content).strip()

    first_content = request_content(payload)
    try:
        return _json_from_text(first_content)
    except OllamaCloudError:
        # Some cloud model responses ignore schema mode or return a preamble. Retry once
        # with simpler JSON mode and a concise repair instruction; keep the audio evidence/images.
        retry_payload = dict(payload)
        retry_payload["format"] = "json"
        retry_payload["messages"] = [
            {"role": "system", "content": "Return exactly one valid JSON object. No Markdown, no explanation. "
             "Required keys: title, description, hashtags (array), tags (array), category_id, reason."},
            {**user_message, "content": json.dumps({"instruction": "Create accurate YouTube Live metadata from these audio features, spectrogram evidence and research. Include relevant hashtags in title and description. Do not invent song or artist names.", "evidence": evidence}, ensure_ascii=False)},
        ]
        retry_payload["options"] = {"temperature": 0.1, "num_predict": 1200}
        retry_content = request_content(retry_payload)
        try:
            return _json_from_text(retry_content)
        except OllamaCloudError as exc:
            raise OllamaCloudError(
                "Ollama Cloud tidak menghasilkan JSON metadata yang bisa dibaca setelah dua percobaan. "
                "Pastikan model Live vision sesuai dan kuota tersedia, lalu coba lagi."
            ) from exc


def generate_song_draft(api_key: str, model: str, idea: str, language: str = "Bahasa Indonesia",
                        genre: str = "Pop", mood: str = "Hangat", vocal: str = "mixed",
                        instrumental: bool = False) -> dict:
    """Draft original, editable song metadata and lyrics for the Music Studio."""
    if not api_key.strip():
        raise OllamaCloudError("API key Ollama Cloud belum diisi.")
    idea = str(idea or "").strip()[:1200]
    if len(idea) < 8:
        raise OllamaCloudError("Ceritakan ide lagu dengan sedikit lebih detail (minimal 8 karakter).")
    brief = {"idea": idea, "lyric_language": str(language or "Bahasa Indonesia")[:60],
             "genre": str(genre or "Pop")[:120], "mood": str(mood or "Hangat")[:120],
             "vocal": str(vocal or "mixed")[:30], "instrumental": bool(instrumental)}
    system = (
        "You are an Indonesian songwriter and music arranger. Create an original song concept; never quote or imitate "
        "existing lyrics or a named artist. Return exactly one JSON object with string keys title, lyrics, style. "
        "The title must be <=80 characters, lyrics <=5000 characters, and style <=1000 characters. "
        "Use clear song sections such as [Verse], [Pre-Chorus], [Chorus], and [Bridge] where appropriate. "
        "Write lyrics in the requested language. Keep style as a concise Suno-compatible production prompt describing "
        "genre, instruments, mood, tempo, vocal character, and arrangement. For instrumental=true, lyrics must be an empty string. "
        "Do not include Markdown fences or commentary."
    )
    prompt = "Create a song draft from this brief:\n" + json.dumps(brief, ensure_ascii=False)
    content = _chat(api_key, model or "gpt-oss:20b", system, prompt)
    try:
        result = _json_from_text(content)
    except OllamaCloudError:
        retry_system = "Return only one valid JSON object with string keys title, lyrics, style. No Markdown."
        content = _chat(api_key, model or "gpt-oss:20b", retry_system,
                        prompt + "\nOutput requirements: title <=80 characters; lyrics <=5000; style <=1000. "
                        "Original lyrics only. instrumental=" + str(bool(instrumental)).lower())
        result = _json_from_text(content)
    draft = {key: str(result.get(key) or "").strip() for key in ("title", "lyrics", "style")}
    if not draft["title"] or not draft["style"]:
        raise OllamaCloudError("Draf lagu Ollama harus memiliki judul dan style musik.")
    if instrumental:
        draft["lyrics"] = ""
    if len(draft["title"]) > 80 or len(draft["lyrics"]) > 5000 or len(draft["style"]) > 1000:
        raise OllamaCloudError("Draf lagu melewati batas panjang. Coba ulangi dengan ide yang lebih ringkas.")
    return draft


def _normalize_live_metadata(data: dict) -> dict:
    hashtags = []
    for item in data.get("hashtags", []):
        tag = re.sub(r"[^\w#]+", "", str(item).strip(), flags=re.UNICODE)
        if tag and not tag.startswith("#"):
            tag = "#" + tag
        if tag and tag.lower() not in {x.lower() for x in hashtags}:
            hashtags.append(tag)
    hashtags = hashtags[:8]
    title = re.sub(r"\s+", " ", str(data.get("title") or "Live 24 Jam")).strip()
    title_tags = re.findall(r"#[\w]+", title, flags=re.UNICODE)
    if hashtags and not any(tag.lower() in {x.lower() for x in title_tags} for tag in hashtags):
        tag = hashtags[0]
        title = f"{title[:max(1, 100 - len(tag) - 1)].rstrip()} {tag}"
    description = str(data.get("description") or "").strip()
    existing = {tag.lower() for tag in re.findall(r"#[\w]+", description, flags=re.UNICODE)}
    missing = [tag for tag in hashtags if tag.lower() not in existing]
    if missing:
        description = (description + "\n\n" + " ".join(missing)).strip()
    data["title"] = title[:100]
    data["description"] = description[:5000]
    data["hashtags"] = hashtags
    data["tags"] = [str(x).replace("#", "").strip()[:50] for x in data.get("tags", []) if str(x).strip()][:20]
    return data


def generate_live_metadata(api_key: str, transcript: str = "", audio_name: str = "", context: str = "",
                           model: str = "gemma4:31b", youtube_access_token: str = "",
                           audio_analysis: dict | None = None,
                           spectrogram_paths: list[str | Path] | None = None) -> dict:
    """Create grounded Live title/description using audio evidence, web research, and spectrograms."""
    topic = (context or audio_name or transcript or "musik live").strip()
    if audio_analysis is not None:
        track_query = ", ".join(str(x.get("filename", "")) for x in audio_analysis.get("tracks", []))
        query = (context or track_query or "musik live")[:500]
        research = _web_research(api_key, query)
        youtube_refs = _youtube_references(youtube_access_token, query)
        clean_analysis = {k: v for k, v in audio_analysis.items() if k != "tracks"}
        clean_analysis["tracks"] = [
            {k: v for k, v in track.items() if k != "spectrogram"}
            for track in audio_analysis.get("tracks", [])
        ]
        data = _chat_live_metadata(api_key, model, topic, clean_analysis, research, youtube_refs,
                                   spectrogram_paths or [])
        data = _normalize_live_metadata(data)
        data["research"] = research
        data["youtube_references"] = youtube_refs
        return data

    refs = _youtube_references(youtube_access_token, (transcript or audio_name or topic)[:500])
    prompt = f"""Buat metadata YouTube Live Bahasa Indonesia berdasarkan audio.
Analisis/transkrip audio: {(transcript or '')[:10000] or '[tidak tersedia]'}
Nama file: {audio_name or '[tidak tersedia]'}\nKonteks: {topic}
Referensi populer (jangan salin): {json.dumps([x['title'] for x in refs], ensure_ascii=False)}
Balas JSON berisi youtube_title (maks 100 karakter), youtube_description (CTA + 3-6 hashtag relevan). Hindari clickbait/spam."""
    data = _json_from_text(_chat(api_key, model,
        "Ahli metadata YouTube Live Indonesia. Jujur, relevan, tanpa janji viral atau klaim tak terbukti.", prompt))
    title = re.sub(r"\s+", " ", str(data.get("youtube_title") or "Live 24 Jam")).strip()
    description = str(data.get("youtube_description") or "").strip()
    data["youtube_title"] = title[:100]
    data["youtube_description"] = description[:5000]
    data["youtube_references"] = refs
    return data


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
