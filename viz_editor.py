"""Library-based music visualizer video renderer."""
from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path


DATA_DIR = Path("data").resolve()
AUDIO_DIR = DATA_DIR / "audios"
VIDEO_DIR = DATA_DIR / "videos"
OUTPUT_DIR = DATA_DIR / "output"
IMAGE_DIR = DATA_DIR / "thumbnails"
TMP_DIR = DATA_DIR / "tmp"

ASPECTS = {"16:9 · YouTube": (1920, 1080), "9:16 · Shorts/Reels": (1080, 1920), "1:1 · Kotak": (1080, 1080)}
VISUALIZER_MODES = {"Bars": "bars", "Waveform": "waveform", "Tanpa visualizer": "none"}


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = Path(path).resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


def validate_library_assets(audio_path: str | Path, background_path: str | Path,
                            output_path: str | Path) -> tuple[Path, Path, Path]:
    """Accept only audio/background inputs and MP4 output inside app Library folders."""
    audio = Path(audio_path).resolve()
    background = Path(background_path).resolve()
    output = Path(output_path).resolve()
    audio_exts = {".mp3", ".aac", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".wma", ".aiff"}
    background_exts = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v", ".wmv", ".ts", ".3gp",
                       ".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    if not _inside(audio, (AUDIO_DIR,)) or not audio.is_file() or audio.suffix.lower() not in audio_exts:
        raise ValueError("Audio harus dipilih dari Library → Audio.")
    if (not _inside(background, (VIDEO_DIR, OUTPUT_DIR, IMAGE_DIR)) or not background.is_file()
            or background.suffix.lower() not in background_exts):
        raise ValueError("Background harus dipilih dari Library → Video, Hasil, atau Thumbnail.")
    if not _inside(output, (OUTPUT_DIR,)) or output.suffix.lower() != ".mp4":
        raise ValueError("File hasil harus berupa MP4 di Library → Hasil.")
    return audio, background, output


def preview_background_data_url(background_path: str | Path) -> str:
    """Create a compact JPEG data URL preview from a Library image or video poster frame."""
    background = Path(background_path).resolve()
    if not _inside(background, (VIDEO_DIR, OUTPUT_DIR, IMAGE_DIR)) or not background.is_file():
        return ""
    try:
        from PIL import Image
        if background.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            image = Image.open(background)
        else:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                return ""
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "0", "-i", str(background),
                 "-an", "-frames:v", "1", "-vf", "scale=1280:720:force_original_aspect_ratio=decrease",
                 "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                capture_output=True, timeout=20,
            )
            if result.returncode != 0 or not result.stdout:
                return ""
            image = Image.open(io.BytesIO(result.stdout))
        image.seek(0)
        if image.mode in {"RGBA", "LA"}:
            rgba = image.convert("RGBA")
            base = Image.new("RGB", rgba.size, (10, 12, 16))
            base.paste(rgba, mask=rgba.getchannel("A"))
            image = base
        else:
            image = image.convert("RGB")
        image.thumbnail((1280, 720), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=78, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:  # noqa: BLE001
        return ""


def _hex_color(value: str, fallback: str = "#12e6ca") -> str:
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(value or ""))
    return "0x" + match.group(1).upper() if match else "0x" + fallback.lstrip("#").upper()


def _escape_filter_path(value: str | Path) -> str:
    """Escape a filesystem path for a quoted FFmpeg filter option."""
    text = str(value).replace("\\", "\\\\")
    for char in (":", "'", ",", "[", "]", "%"):
        text = text.replace(char, "\\" + char)
    return text


def _effect_filter(effect: str) -> str:
    presets = {
        "Normal": "eq=brightness=0:contrast=1:saturation=1",
        "Warm": "colorbalance=rs=0.10:gs=0.02:bs=-0.10",
        "Cool": "colorbalance=rs=-0.08:gs=0.01:bs=0.12",
        "Mono": "hue=s=0",
        "Dream": "gblur=sigma=1.2,eq=brightness=0.025:contrast=0.96:saturation=0.78",
        "Gelap": "eq=brightness=-0.12:contrast=1.05:saturation=0.95",
    }
    return presets.get(str(effect), presets["Normal"])


def build_ffmpeg_command(audio_path: str | Path, background_path: str | Path,
                         output_path: str | Path, options: dict | None = None,
                         title_file: str | Path | None = None,
                         subtitle_file: str | Path | None = None) -> list[str]:
    """Build a deterministic FFmpeg command; caller validates paths and writes text files."""
    opts = dict(options or {})
    aspect = str(opts.get("aspect", "16:9 · YouTube"))
    width, height = ASPECTS.get(aspect, ASPECTS["16:9 · YouTube"])
    bg_path = Path(background_path)
    is_image = bg_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(audio_path)]
    if is_image:
        command += ["-loop", "1", "-framerate", "30", "-i", str(bg_path)]
    else:
        command += ["-stream_loop", "-1", "-i", str(bg_path)]

    filters = [
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
        f"setsar=1,fps=30,{_effect_filter(str(opts.get('background_effect', 'Normal')))}[base]",
    ]
    current = "base"
    mode = str(opts.get("visualizer", "bars"))
    if mode != "none":
        viz_height = max(64, min(int(height * 0.45), int(height * float(opts.get("visualizer_size", 18)) / 100)))
        viz_width = int(width * 0.88)
        color_a = _hex_color(opts.get("color_a", "#12e6ca"))
        color_b = _hex_color(opts.get("color_b", "#ff3cac"))
        if mode == "waveform":
            viz_source = f"showwaves=s={viz_width}x{viz_height}:mode=cline:rate=30:colors={color_a}"
        else:
            viz_source = (f"showfreqs=s={viz_width}x{viz_height}:mode=bar:cmode=combined:"
                          f"ascale=log:fscale=log:win_size=2048:rate=30:colors={color_a}|{color_b}")
        pos = str(opts.get("visualizer_position", "Bawah"))
        default_viz_y = {"Atas": 12, "Tengah": 50, "Bawah": 84}.get(pos, 84)
        viz_x_pct = max(0.0, min(100.0, float(opts.get("visualizer_x_pct", 50))))
        viz_y_pct = max(0.0, min(100.0, float(opts.get("visualizer_y_pct", default_viz_y))))
        x_expr = f"'max(0,min(W-w,W*{viz_x_pct / 100:.4f}-w/2))'"
        y_expr = f"'max(0,min(H-h,H*{viz_y_pct / 100:.4f}-h/2))'"
        filters.append(
            f"[0:a]{viz_source},format=rgba,colorkey=black:0.12:0.08[viz];"
            f"[base][viz]overlay=x={x_expr}:y={y_expr}:shortest=1[with_viz]"
        )
        current = "with_viz"

    title = str(opts.get("title", "")).strip()
    subtitle = str(opts.get("subtitle", "")).strip()
    text_color = _hex_color(opts.get("text_color", "#ffffff"))
    font_size = max(16, min(160, int(opts.get("font_size", 64))))
    text_position = str(opts.get("text_position", "Tengah"))
    default_title_y = {"Atas": 12, "Tengah": 48, "Bawah": 76}.get(text_position, 48)
    title_x_pct = max(0.0, min(100.0, float(opts.get("title_x_pct", 50))))
    title_y_pct = max(0.0, min(100.0, float(opts.get("title_y_pct", default_title_y))))
    title_x_target = f"'max(0,min(w-text_w,w*{title_x_pct / 100:.4f}-text_w/2))'"
    title_y_expr = f"'max(0,min(h-text_h,h*{title_y_pct / 100:.4f}-text_h/2))'"
    effect = str(opts.get("text_effect", "Bayangan"))
    animation = str(opts.get("text_animation", "Tanpa animasi"))
    font_name = str(opts.get("font", "DejaVu Sans"))
    if font_name not in {"Sans", "Serif", "Monospace", "DejaVu Sans", "DejaVu Sans Mono"}:
        font_name = "DejaVu Sans"
    text_opacity = max(0.1, min(1.0, float(opts.get("text_opacity", 1.0))))
    title_x = title_x_target
    title_font_size = str(font_size)
    animation_style = ""
    if animation == "Fade":
        animation_style = ":alpha='min(1,t/1.2)'"
    elif animation == "Slide":
        title_x = f"'if(lt(t,1),-text_w+(w+text_w)*t,max(0,min(w-text_w,w*{title_x_pct / 100:.4f}-text_w/2)))'"
    elif animation == "Denyut":
        title_font_size = f"'{font_size}*(1+0.06*sin(2*PI*t))'"
    text_style = ""
    if effect == "Outline":
        text_style = ":borderw=3:bordercolor=black@0.9"
    elif effect == "Glow":
        text_style = f":borderw=3:bordercolor={text_color}@0.65:shadowcolor={text_color}@0.6:shadowx=0:shadowy=0"
    elif effect == "Bayangan":
        text_style = ":shadowcolor=black@0.8:shadowx=3:shadowy=3"
    elif effect == "Emboss":
        text_style = ":borderw=1:bordercolor=white@0.55:shadowcolor=black@0.9:shadowx=2:shadowy=2"

    draw_count = 0
    if title and title_file:
        filters.append(
            f"[{current}]drawtext=textfile='{_escape_filter_path(title_file)}':expansion=none:"
            f"font='{font_name}':fontcolor={text_color}@{text_opacity:.2f}:fontsize={title_font_size}:x={title_x}:y={title_y_expr}:"
            f"box=1:boxcolor=black@0.22:boxborderw=14{text_style}{animation_style}[text{draw_count}]"
        )
        current = f"text{draw_count}"
        draw_count += 1
    if subtitle and subtitle_file:
        default_sub_y = 22 if text_position == "Atas" else 86
        subtitle_x_pct = max(0.0, min(100.0, float(opts.get("subtitle_x_pct", 50))))
        subtitle_y_pct = max(0.0, min(100.0, float(opts.get("subtitle_y_pct", default_sub_y))))
        subtitle_x = f"'max(0,min(w-text_w,w*{subtitle_x_pct / 100:.4f}-text_w/2))'"
        subtitle_y = f"'max(0,min(h-text_h,h*{subtitle_y_pct / 100:.4f}-text_h/2))'"
        filters.append(
            f"[{current}]drawtext=textfile='{_escape_filter_path(subtitle_file)}':expansion=none:"
            f"font='{font_name}':fontcolor=white@{text_opacity:.2f}:fontsize={max(14, font_size // 2)}:"
            f"x={subtitle_x}:y={subtitle_y}:shadowcolor=black@0.8:shadowx=2:shadowy=2[text{draw_count}]"
        )
        current = f"text{draw_count}"
        draw_count += 1
    filters.append(f"[{current}]format=yuv420p[vout]")

    command += [
        "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", "30", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-shortest",
        "-movflags", "+faststart", str(output_path),
    ]
    return command


def render_visualizer(job: dict, *, audio_path: str | Path, background_path: str | Path,
                      output_path: str | Path, options: dict, disk_min_gb: float = 0.5) -> tuple[bool, str]:
    """Render an MP4 in a background JobRunner worker and atomically publish to Library/Output."""
    try:
        audio, background, output = validate_library_assets(audio_path, background_path, output_path)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg tidak ditemukan.")
        output.parent.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(output.parent).free / 1024**3
        if free_gb < max(0.0, float(disk_min_gb)):
            raise RuntimeError(f"Disk kosong {free_gb:.2f} GB, di bawah batas {float(disk_min_gb):.2f} GB.")
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="viz_", dir=TMP_DIR) as temp_dir:
            temp_dir = Path(temp_dir)
            staged_output = temp_dir / output.name
            title = str(options.get("title", "")).strip()[:240]
            subtitle = str(options.get("subtitle", "")).strip()[:300]
            title_file = temp_dir / "title.txt" if title else None
            subtitle_file = temp_dir / "subtitle.txt" if subtitle else None
            if title_file:
                title_file.write_text(title, encoding="utf-8")
            if subtitle_file:
                subtitle_file.write_text(subtitle, encoding="utf-8")
            command = build_ffmpeg_command(audio, background, staged_output, options, title_file, subtitle_file)
            command[0] = ffmpeg
            result = subprocess.run(command, capture_output=True, text=True, timeout=24 * 60 * 60)
            if result.returncode != 0 or not staged_output.is_file() or staged_output.stat().st_size == 0:
                error = (result.stderr or "FFmpeg tidak menghasilkan file.").strip().splitlines()[-1][:400]
                raise RuntimeError(f"Render gagal: {error}")
            os.replace(staged_output, output)
        return True, f"Video selesai disimpan ke Library → Hasil/{output.name}"
    except Exception as exc:  # noqa: BLE001
        return False, f"VIZ gagal: {type(exc).__name__}: {exc}"
