import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import viz_editor


class VizEditorTests(unittest.TestCase):
    def test_build_command_uses_library_background_canvas_and_audio_visualizer(self):
        command = viz_editor.build_ffmpeg_command(
            "data/audios/song.mp3", "data/thumbnails/cover.png", "data/output/song_viz.mp4",
            {
                "aspect": "9:16 · Shorts/Reels", "visualizer": "bars", "visualizer_position": "Bawah",
                "visualizer_x_pct": 27, "visualizer_y_pct": 72, "visualizer_size": 18,
                "color_a": "#12e6ca", "color_b": "#ff3cac", "title": "A title",
                "title_x_pct": 31, "title_y_pct": 43, "text_effect": "Glow",
                "text_animation": "Fade", "font_size": 64,
            },
            "/tmp/viz/title.txt",
        )
        self.assertIn("-loop", command)
        self.assertIn("-filter_complex", command)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920", graph)
        self.assertIn("showfreqs=", graph)
        self.assertIn("overlay=x='max(0,min(W-w,W*0.2700-w/2))':y='max(0,min(H-h,H*0.7200-h/2))'", graph)
        self.assertIn("textfile='/tmp/viz/title.txt'", graph)
        self.assertIn("x='max(0,min(w-text_w,w*0.3100-text_w/2))'", graph)
        self.assertIn("alpha='min(1,t/1.2)'", graph)
        self.assertIn("-map", command)
        self.assertEqual(command[-1], "data/output/song_viz.mp4")

    def test_build_command_supports_video_background_waveform_and_safe_color_fallback(self):
        command = viz_editor.build_ffmpeg_command(
            "track.wav", "background.mp4", "result.mp4",
            {"visualizer": "waveform", "color_a": "not-a-color", "aspect": "1:1 · Kotak"},
        )
        self.assertIn("-stream_loop", command)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=1080:1080", graph)
        self.assertIn("showwaves=", graph)
        self.assertIn("colors=0x12E6CA", graph)
        self.assertNotIn("not-a-color", graph)

    def test_preview_background_converts_library_image_to_compact_data_url(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            image_dir = Path(tmp)
            image_path = image_dir / "cover.png"
            Image.new("RGB", (32, 18), (20, 100, 180)).save(image_path)
            with patch.object(viz_editor, "IMAGE_DIR", image_dir):
                preview = viz_editor.preview_background_data_url(image_path)
        self.assertTrue(preview.startswith("data:image/jpeg;base64,"))

    def test_validate_only_accepts_audio_and_backgrounds_inside_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_dir, video_dir, output_dir, image_dir = [root / name for name in ("audio", "video", "output", "images")]
            for folder in (audio_dir, video_dir, output_dir, image_dir):
                folder.mkdir()
            audio = audio_dir / "song.mp3"
            image = image_dir / "cover.png"
            output = output_dir / "viz.mp4"
            audio.touch()
            image.touch()
            with patch.multiple(viz_editor, AUDIO_DIR=audio_dir, VIDEO_DIR=video_dir,
                                OUTPUT_DIR=output_dir, IMAGE_DIR=image_dir):
                self.assertEqual(viz_editor.validate_library_assets(audio, image, output),
                                 (audio.resolve(), image.resolve(), output.resolve()))
                with self.assertRaises(ValueError):
                    viz_editor.validate_library_assets(root / "outside.mp3", image, output)


if __name__ == "__main__":
    unittest.main()
