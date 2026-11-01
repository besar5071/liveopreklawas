"""Background generation and YouTube upload services for vertical Shorts."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import services as svc
import ytauth

RECORD_FILE = svc.BASE / "shorts_history.json"
CLIPDROP_URL = "https://clipdrop-api.co/text-to-image/v1"


def _safe_text(value: Any, limit: int = 5000) -> str:
    return str(value or "").strip()[:limit]


def _records() -> list[dict]:
    try:
        data = json.loads(RECORD_FILE.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_records(rows: list[dict]) -> None:
    RECORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RECORD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows[-100:], ensure_ascii=False, indent=2), "utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(RECORD_FILE)


def list_recent_shorts() -> list[dict]:
    return [r for r in reversed(_records()) if r.get("video_path")]


def update_short_output_entry(video_path: Path, values: dict) -> None:
    target = str(Path(video_path).resolve())
    rows = _records()
    for row in rows:
        if str(Path(row.get("video_path", "")).resolve()) == target:
            row.update(values)
            break
    _save_records(rows)


def _font(size: int, bold: bool = False):
    choices = (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]
               if bold else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"])
    for path in choices:
        if Path(path).is_file():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        if not paragraph.strip():
            lines.append("")
            continue
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}".strip()
            if line and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
    return lines


def _compose_card(bg_path: Path, header: str, body: str, watermark: str, output: Path) -> None:
    im = Image.open(bg_path).convert("RGB").resize((1080, 1920), Image.Resampling.LANCZOS)
    im = im.filter(ImageFilter.GaussianBlur(1.2))
    # Darken and add a gentle gradient to ensure readable text.
    overlay = Image.new("RGBA", im.size, (5, 8, 22, 95))
    im = Image.alpha_composite(im.convert("RGBA"), overlay)
    shade = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for y in range(250, 1700):
        alpha = int(135 * min(1, max(0, (y - 250) / 260), max(0, (1700 - y) / 280)))
        sd.line((0, y, 1080, y), fill=(5, 7, 18, alpha))
    im = Image.alpha_composite(im, shade)
    d = ImageDraw.Draw(im)
    margin, max_w = 102, 876
    title_font = _font(72, True)
    body_font = _font(52, False)
    title_lines = _wrap(d, header, title_font, max_w)
    body_lines = _wrap(d, body, body_font, max_w)
    # Shrink body until all text fits safely in the 9:16 frame.
    while body_font.size > 35 and len(body_lines) * (body_font.size + 22) + len(title_lines) * (title_font.size + 14) > 1250:
        body_font = _font(body_font.size - 2, False)
        body_lines = _wrap(d, body, body_font, max_w)
    title_h = len(title_lines) * 88 + 40
    body_h = len(body_lines) * (body_font.size + 22)
    y = max(310, (1920 - (title_h + body_h)) // 2)
    # translucent panel
    panel_bottom = min(1610, y + title_h + body_h + 70)
    d.rounded_rectangle((48, y - 38, 1032, panel_bottom), radius=38, fill=(8, 10, 22, 155), outline=(255, 255, 255, 50), width=2)
    y += 3
    for line in title_lines:
        d.text((margin + 2, y + 4), line, font=title_font, fill=(0, 0, 0, 200), stroke_width=2)
        d.text((margin, y), line, font=title_font, fill=(255, 255, 255, 255), stroke_width=1, stroke_fill=(20, 20, 20, 180))
        y += 88
    y += 18
    for line in body_lines:
        d.text((margin + 2, y + 3), line, font=body_font, fill=(0, 0, 0, 210))
        d.text((margin, y), line, font=body_font, fill=(255, 255, 255, 255), stroke_width=1, stroke_fill=(15, 15, 15, 130))
        y += body_font.size + 22
    if watermark:
        wmfont = _font(30, True)
        box = d.textbbox((0, 0), watermark, font=wmfont)
        d.rounded_rectangle((56, 1790, 56 + box[2] + 42, 1850), radius=18, fill=(0, 0, 0, 120))
        d.text((77, 1803), watermark, font=wmfont, fill=(255, 255, 255, 230))
    im.convert("RGB").save(output, "PNG", optimize=True)


def _duration(audio_path: Path | None) -> float:
    if audio_path and audio_path.is_file():
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
                           capture_output=True, text=True, timeout=45)
        try:
            return max(1.0, min(float(r.stdout.strip()), 180.0))
        except (ValueError, TypeError):
            pass
    return 30.0


def _render(job: dict, *, clipdrop_api_key: str, header: str, body: str, clipdrop_prompt: str,
            audio_path: str | Path | None = None, audio_volume: int = 100, disk_min_gb: float = 0.5,
            youtube_title: str = "", youtube_description: str = "", youtube_tags: str = "", output_name: str = "",
            topic_key: str = "", custom_topic: str = "", theme: str = "", watermark: str = "",
            youtube_references: list | None = None, trending_keywords: list | None = None,
            auto_upload_youtube: bool = False, youtube_access_token: str = "", upload_privacy: str = "public",
            upload_channel_title: str = "", **_: Any):
    if not clipdrop_api_key.strip():
        return False, "API key Clipdrop belum diisi."
    if not _safe_text(header) or not _safe_text(body):
        return False, "Judul dan isi kartu wajib diisi."
    if not _safe_text(clipdrop_prompt):
        return False, "Prompt latar Clipdrop wajib diisi."
    free = svc.disk_info()["free_gb"]
    if free < disk_min_gb:
        return False, f"Sisa disk {free:.2f} GB di bawah batas {disk_min_gb:.2f} GB."
    try:
        response = requests.post(CLIPDROP_URL, headers={"x-api-key": clipdrop_api_key.strip()},
                                 data={"prompt": clipdrop_prompt[:1000]}, timeout=180)
        if not response.ok:
            return False, f"Clipdrop menolak permintaan ({response.status_code}): {response.text[:400]}"
        if not response.headers.get("Content-Type", "").startswith("image/"):
            return False, "Respons Clipdrop bukan gambar. Periksa API key dan kuota."
        stage = Path(tempfile.mkdtemp(prefix="short_", dir=svc.TMP))
        try:
            bg = stage / "background.png"
            bg.write_bytes(response.content)
            card = stage / "card.png"
            _compose_card(bg, header, body, watermark, card)
            audio = Path(audio_path) if audio_path else None
            if audio and not audio.is_file():
                return False, f"File audio tidak ditemukan: {audio}"
            dur = _duration(audio)
            name = re.sub(r"[^\w-]+", "_", (output_name or youtube_title or header).encode("ascii", "ignore").decode()).strip("_")[:64] or f"short_{int(time.time())}"
            output = svc.unique_path(svc.DIRS["output"], name + ".mp4")
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-loop", "1", "-framerate", "30", "-i", str(card)]
            if audio:
                cmd += ["-i", str(audio), "-t", f"{dur:.3f}", "-map", "0:v:0", "-map", "1:a:0", "-af", f"volume={max(0, min(int(audio_volume), 200)) / 100:.2f}", "-c:a", "aac", "-b:a", "192k"]
            else:
                cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100", "-t", f"{dur:.3f}", "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "128k"]
            cmd += ["-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-movflags", "+faststart", str(output)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=max(180, int(dur * 5)))
            if result.returncode or not output.is_file() or output.stat().st_size < 10000:
                output.unlink(missing_ok=True)
                return False, "FFmpeg gagal membuat Shorts: " + (result.stderr or "output kosong")[-450:]
            record = {"video_path": str(output.resolve()), "header": header, "body": body,
                      "clipdrop_prompt": clipdrop_prompt, "youtube_title": youtube_title or header,
                      "youtube_description": youtube_description, "youtube_tags": [x.strip() for x in youtube_tags.split(",") if x.strip()],
                      "topic_key": topic_key, "topic_label": custom_topic or topic_key, "theme": theme,
                      "watermark": watermark, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "youtube_references": (youtube_references or [])[:5],
                      "trending_keywords": (trending_keywords or [])[:8]}
            rows = _records()
            rows.append(record)
            _save_records(rows)
            if auto_upload_youtube:
                if not youtube_access_token:
                    return False, f"Shorts berhasil dirender {output.name}, tetapi upload otomatis gagal: token YouTube kosong."
                uploaded = ytauth.upload_video(youtube_access_token, output, record["youtube_title"], youtube_description,
                                               record["youtube_tags"], upload_privacy)
                record.update(uploaded, uploaded_channel=upload_channel_title, uploaded_privacy=upload_privacy,
                              uploaded_at=time.strftime("%Y-%m-%d %H:%M:%S"))
                _save_records([*rows[:-1], record])
            return True, f"{output.name} · {dur:.0f} dtk · 1080×1920" + (f" · terupload ke {upload_channel_title}" if auto_upload_youtube else "")
        finally:
            import shutil
            shutil.rmtree(stage, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def submit_viral_short_video(jobs, **kwargs):
    return jobs.submit("Viral Shorts · Render 9:16", lambda job: _render(job, **kwargs))


def submit_upload_short_to_youtube(jobs, *, video_path: Path, youtube_access_token: str,
                                   channel_title: str = "YouTube", privacy: str = "public", title: str = "",
                                   description: str = "", tags: str = "", auto_ollama_metadata: bool = False,
                                   ollama_api_key: str = "", ollama_model: str = "", header: str = "", body: str = "",
                                   topic_key: str = "", custom_topic: str = "", watermark: str = "", **kwargs):
    video_path = Path(video_path)

    def run(job):
        try:
            final_title, final_desc, final_tags = title, description, tags
            metadata = None
            if auto_ollama_metadata:
                import ollama_cloud
                metadata = ollama_cloud.generate_viral_short_upload_metadata(
                    ollama_api_key, header=header, body=body, topic_key=topic_key, custom_topic=custom_topic,
                    watermark=watermark, model=ollama_model, youtube_access_token=youtube_access_token)
                final_title = metadata["youtube_title"]
                final_desc = metadata["youtube_description"]
                final_tags = ", ".join(metadata.get("youtube_tags") or [])
            tag_list = [x.strip() for x in str(final_tags).split(",") if x.strip()]
            result = ytauth.upload_video(youtube_access_token, video_path, final_title, final_desc, tag_list, privacy)
            recs = _records()
            matched = False
            for rec in recs:
                if str(Path(rec.get("video_path", "")).resolve()) == str(video_path.resolve()):
                    rec.update(result, youtube_title=final_title, youtube_description=final_desc,
                               youtube_tags=tag_list, uploaded_channel=channel_title, uploaded_privacy=privacy,
                               uploaded_at=time.strftime("%Y-%m-%d %H:%M:%S"))
                    if metadata:
                        rec.update({k: metadata.get(k) for k in ("youtube_references", "trending_keywords")})
                    matched = True
                    break
            if not matched:
                recs.append({"video_path": str(video_path.resolve()), "youtube_title": final_title,
                             "youtube_description": final_desc, "youtube_tags": tag_list,
                             "header": header, "body": body, "topic_key": topic_key,
                             "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), **result,
                             "uploaded_channel": channel_title, "uploaded_privacy": privacy,
                             "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            _save_records(recs)
            return True, f"Terupload: {result['shorts_url']}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    return jobs.submit("Upload Shorts → YouTube: " + (video_path.name[:50]), run)
