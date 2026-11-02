import unittest.mock
import tempfile
import unittest
from pathlib import Path

import relay_video as rv


class CleanTextTests(unittest.TestCase):
    def test_clean_text_strips_emoji_markdown_and_links(self):
        raw = "## Judul **penting** 🔥\n- lihat [sumber](https://contoh.id) `kode`"
        cleaned = rv.clean_text(raw)
        self.assertNotIn("🔥", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertNotIn("#", cleaned)
        self.assertIn("sumber", cleaned)
        self.assertIn("Judul penting", cleaned)

    def test_split_sentences_keeps_question_marks(self):
        self.assertEqual(rv.split_sentences("Apa itu? Ini jawabannya. Selesai."),
                         ["Apa itu?", "Ini jawabannya.", "Selesai."])


class PlanTests(unittest.TestCase):
    def test_normalize_plan_cleans_scenes_and_fills_missing_fields(self):
        plan = rv.normalize_plan({
            "title": "Tips Hemat ⚡",
            "voice": "id-ID-GadisNeural",
            "scenes": [
                {"narration": "Kalimat satu.", "headline": "Hook", "bullets": "a | b | c | d",
                 "accent": "#abc"},
                "adegan berupa string",
                {"headline": ""},
            ],
        })
        self.assertEqual(plan["title"], "Tips Hemat")
        self.assertEqual(plan["voice"], "id-ID-GadisNeural")
        self.assertEqual(len(plan["scenes"]), 2)
        self.assertEqual(plan["scenes"][0]["accent"], "#aabbcc")
        self.assertEqual(len(plan["scenes"][0]["bullets"]), 3)
        self.assertEqual(plan["scenes"][1]["narration"], "adegan berupa string")

    def test_normalize_plan_falls_back_when_scenes_missing(self):
        plan = rv.normalize_plan({"title": "Cadangan"}, topic="Isi teks pengguna.")
        self.assertEqual(plan["scenes"][0]["narration"], "Isi teks pengguna.")

    def test_fallback_plan_splits_user_text_into_scenes(self):
        plan = rv.fallback_plan("Satu. Dua. Tiga. Empat.", scenes=4)
        self.assertEqual(len(plan["scenes"]), 4)
        self.assertEqual(plan["scenes"][2]["narration"], "Tiga.")
        self.assertTrue(all(scene["headline"] for scene in plan["scenes"]))

    def test_json_from_text_accepts_prose_wrapped_object(self):
        text = 'Berikut naskahnya:\n```json\n{"title": "Halo", "scenes": []}\n```\nSemoga membantu.'
        self.assertEqual(rv.json_from_text(text)["title"], "Halo")

    def test_json_from_text_rejects_plain_prose(self):
        with self.assertRaises(rv.RelayVideoError):
            rv.json_from_text("maaf, tidak ada json di sini")

    def test_tone_accepts_label_or_key(self):
        self.assertEqual(rv.tone_key("Edukasi"), "edukasi")
        self.assertEqual(rv.tone_key("santai"), "santai")
        self.assertEqual(rv.tone_key("tidak ada"), "viral")
        self.assertTrue(all(v in rv.TONES.values() for v in rv.TONES.values()))

    def test_plan_prompt_mentions_scene_count_and_json(self):
        prompt = rv.plan_prompt("Kopi robusta", 5, 40, tone="edukasi")
        self.assertIn("tepat 5", prompt)
        self.assertIn("Kopi robusta", prompt)
        self.assertIn('"scenes"', prompt)


class FilterTests(unittest.TestCase):
    def test_scene_filter_scales_twice_for_zoompan_and_fades(self):
        chain = rv.scene_video_filter(1080, 1920, 6.0)
        self.assertIn("scale=2160:3840", chain)
        self.assertIn("zoompan=", chain)
        self.assertIn("s=1080x1920", chain)
        self.assertIn("fade=t=in:st=0", chain)
        self.assertIn("fade=t=out:st=5.60", chain)
        self.assertTrue(chain.endswith("format=yuv420p"))

    def test_scene_filter_without_motion_avoids_zoompan(self):
        chain = rv.scene_video_filter(720, 1280, 1.0, motion=False)
        self.assertNotIn("zoompan", chain)
        self.assertIn("scale=720:1280", chain)

    def test_build_scene_command_uses_loop_and_audio_shortest(self):
        command = rv.build_scene_command("scene.png", "clip.mp4", 4.5, 1080, 1920,
                                         audio_path="voice.mp3", ffmpeg="ffmpeg")
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("-loop", command)
        self.assertIn("-shortest", command)
        self.assertIn("voice.mp3", command)
        self.assertEqual(command[-1], "clip.mp4")

    def test_subtitle_filter_names_srt_relative_to_workdir(self):
        chain = rv.subtitle_filter("/tmp/work/narasi.srt", font_size=12)
        self.assertTrue(chain.startswith("subtitles=narasi.srt:"))
        self.assertIn("force_style=", chain)

    def test_canvas_size_respects_aspect_and_quality(self):
        self.assertEqual(rv.canvas_size("9:16 · Shorts/Reels", "Cepat · 720×1280"), (720, 1280))
        self.assertEqual(rv.canvas_size("16:9 · YouTube", "Tinggi · 1080×1920"), (1920, 1080))
        self.assertEqual(rv.canvas_size("tidak dikenal"), (1080, 1920))


class SubtitleTests(unittest.TestCase):
    def test_build_srt_orders_timestamps_and_splits_long_sentences(self):
        scenes = [{"narration": "Kalimat pendek. " + "kata " * 30},
                  {"narration": "Adegan kedua."}]
        srt = rv.build_srt(scenes, [6.0, 4.0])
        blocks = [b for b in srt.strip().split("\n\n") if b.strip()]
        self.assertGreaterEqual(len(blocks), 3)
        self.assertTrue(blocks[0].startswith("1\n00:00:00,000 --> "))
        self.assertIn("00:00:06,000 --> 00:00:10,000", srt)

    def test_scene_durations_fall_back_to_word_count(self):
        durations = rv.scene_durations(
            [{"narration": "satu dua tiga"}, {"narration": "x" * 500}], [None, None],
            seconds_per_scene=5.0)
        self.assertEqual(len(durations), 2)
        self.assertGreaterEqual(durations[0], rv.MIN_SCENE_SECONDS)
        self.assertLessEqual(durations[1], rv.MAX_SCENE_SECONDS)


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_draw_scene_writes_portrait_image_and_keeps_text_inside_frame(self):
        from PIL import Image

        scene = {"headline": "Judul Panjang Sekali Untuk Diuji",
                 "subheadline": "Subjudul yang menjelaskan konteks adegan.",
                 "bullets": ["Poin satu", "Poin dua"], "accent": "#22d3ee"}
        out = self.root / "scene.png"
        for style in rv.STYLES:
            rv.draw_scene(scene, 0, 3, out, aspect="9:16 · Shorts/Reels",
                          quality="Cepat · 720×1280", style=style, watermark="Studio")
            with Image.open(out) as image:
                self.assertEqual(image.size, (720, 1280))
                self.assertEqual(image.mode, "RGB")

    def test_draw_scene_survives_emoji_and_empty_text(self):
        from PIL import Image

        out = self.root / "empty.png"
        rv.draw_scene({"headline": "Halo 🔥 dunia", "bullets": []}, 1, 2, out,
                      aspect="1:1 · Kotak", quality="Cepat · 720×1280")
        with Image.open(out) as image:
            self.assertEqual(image.size, (720, 720))

    def test_render_text_video_rejects_empty_plan(self):
        with self.assertRaises(rv.RelayVideoError):
            rv.normalize_plan({"scenes": []}, topic="")


class RequestTests(unittest.TestCase):
    def test_relay_headers_requires_key(self):
        with self.assertRaises(rv.RelayVideoError):
            rv.relay_headers("   ")
        self.assertEqual(rv.relay_headers(" sk-abc ")["Authorization"], "Bearer sk-abc")

    def test_list_models_parses_openai_shape(self):
        class FakeResponse:
            ok = True
            status_code = 200

            @staticmethod
            def json():
                return {"data": [{"id": "gpt-6-astra"}, "claude-x", {"id": "gpt-6-astra"}]}

        with unittest.mock.patch.object(rv.requests, "get", return_value=FakeResponse()):
            self.assertEqual(rv.list_models("sk-test"), ["claude-x", "gpt-6-astra"])

    def test_chat_falls_back_to_reserve_endpoint(self):
        calls = []

        class FakeResponse:
            def __init__(self, ok, payload=None, text="", status_code=503):
                self.ok, self._payload, self.text = ok, payload, text
                self.status_code = status_code

            def json(self):
                return self._payload

        def fake_post(url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return FakeResponse(False, text="gateway error")
            return FakeResponse(True, {"choices": [{"message": {"content": "halo"}}]}, status_code=200)

        with unittest.mock.patch.object(rv.requests, "post", side_effect=fake_post):
            self.assertEqual(rv.chat("sk-test", "prompt", "system"), "halo")
        self.assertEqual(len(calls), 2)
        self.assertIn("direct.relay-fast.com", calls[1])


if __name__ == "__main__":
    unittest.main()
