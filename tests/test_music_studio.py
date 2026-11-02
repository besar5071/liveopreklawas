import json
import unittest
from unittest.mock import patch

import requests

import music_studio
import ollama_cloud


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.ok = 200 <= status < 300

    def json(self):
        return self.payload


class FakeAudioResponse:
    status_code = 200
    ok = True
    headers = {"Content-Type": "audio/mpeg", "Content-Length": "4"}

    def iter_content(self, chunk_size):
        yield b"audi"
        yield b""

    def close(self):
        pass


class MusicStudioTests(unittest.TestCase):
    def test_create_task_rotates_to_next_key_on_explicit_credit_exhaustion(self):
        keys = ["first-key", "second-key"]
        balances = {music_studio.key_fingerprint(keys[0]): 9, music_studio.key_fingerprint(keys[1]): 20}
        with patch.object(music_studio, "create_task", side_effect=[
            music_studio.KieMusicError("Insufficient credits", 402), ("task_123", {"code": 200}),
        ]) as create:
            result = music_studio.create_task_with_fallback(keys, {"model": "test"}, balances)
        self.assertEqual(result[0], "task_123")
        self.assertEqual(result[2], 2)
        self.assertEqual(result[3], music_studio.key_fingerprint("second-key"))
        self.assertEqual(create.call_count, 2)

    def test_create_task_skips_known_zero_balance_and_never_rotates_on_uncertain_submit(self):
        keys = ["empty-key", "ready-key"]
        balances = {music_studio.key_fingerprint(keys[0]): 0, music_studio.key_fingerprint(keys[1]): 10}
        with patch.object(music_studio, "create_task", return_value=("task_123", {})) as create:
            result = music_studio.create_task_with_fallback(keys, {"model": "test"}, balances)
        self.assertEqual(result[2], 2)
        self.assertEqual(create.call_count, 1)
        with patch.object(music_studio, "create_task", side_effect=music_studio.KieMusicError("insufficient credits; result is uncertain", 503, True)) as create:
            with self.assertRaises(music_studio.KieMusicError) as caught:
                music_studio.create_task_with_fallback(keys, {"model": "test"})
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(create.call_count, 1)

    def test_credit_balance_reads_numeric_credit_from_provider(self):
        response = FakeResponse({"code": 200, "data": 1234.5})
        with patch.object(requests, "get", return_value=response) as get:
            balance = music_studio.get_credit_balance("credit-key")
        self.assertEqual(balance, 1234.5)
        self.assertEqual(get.call_args.args[0], music_studio.CREDIT_URL)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer credit-key")

    def test_credit_balance_accepts_named_balance_and_hides_auth_errors(self):
        with patch.object(requests, "get", return_value=FakeResponse({"code": 200, "data": {"balance": "80"}})):
            self.assertEqual(music_studio.get_credit_balance("credit-key"), 80.0)
        with patch.object(requests, "get", return_value=FakeResponse({"code": 401, "msg": "credit-key unauthorized"}, 401)):
            with self.assertRaises(music_studio.KieMusicError) as caught:
                music_studio.get_credit_balance("credit-key")
        self.assertNotIn("credit-key", str(caught.exception))

    def test_create_task_returns_validated_id_and_uses_bearer_auth(self):
        response = FakeResponse({"code": 200, "data": {"taskId": "task_123"}})
        with patch.object(requests, "request", return_value=response) as request:
            task_id, payload = music_studio.create_task("secret-key", {
                "model": "ai-music-api/generate", "input": {"title": "Test"},
            })
        self.assertEqual(task_id, "task_123")
        self.assertEqual(payload["data"]["taskId"], task_id)
        self.assertEqual(request.call_args.args[:2], ("POST", music_studio.API_BASE + "/createTask"))
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer secret-key")

    def test_uncertain_create_error_does_not_leak_api_key(self):
        response = FakeResponse({"code": 503, "msg": "secret-key upstream unavailable"}, status=503)
        with patch.object(requests, "request", return_value=response):
            with self.assertRaises(music_studio.KieMusicError) as caught:
                music_studio.create_task("secret-key", {"model": "ai-music-api/generate", "input": {}})
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn("secret-key", str(caught.exception))

    def test_download_audio_checks_https_and_returns_mime_and_extension(self):
        with patch.object(requests, "get", return_value=FakeAudioResponse()) as get:
            audio, mime, extension = music_studio.download_audio("https://cdn.example/song.mp3")
        self.assertEqual(audio, b"audi")
        self.assertEqual(mime, "audio/mpeg")
        self.assertEqual(extension, "mp3")
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        with patch.object(requests, "get") as get:
            with self.assertRaises(music_studio.KieMusicError):
                music_studio.download_audio("http://127.0.0.1/private.mp3")
            get.assert_not_called()

    def test_extract_tracks_sanitizes_urls_and_deduplicates(self):
        tracks = music_studio.extract_tracks({"data": {"state": "success", "response": json.dumps({
            "tracks": [
                {"id": "a1", "audio_url": "https://cdn.example/song.mp3", "title": "Song"},
                {"id": "a1", "audio_url": "https://cdn.example/song.mp3", "image_url": "https://cdn.example/cover.jpg"},
                {"id": "bad", "url": "http://unsafe.example/song.mp3"},
            ],
            "resultUrls": ["https://cdn.example/second.mp3", "javascript:alert(1)"],
        })}})
        self.assertEqual(len(tracks), 2)
        by_audio = {track["audio"]: track for track in tracks}
        self.assertEqual(by_audio["https://cdn.example/song.mp3"]["image"], "https://cdn.example/cover.jpg")
        self.assertIn("https://cdn.example/second.mp3", by_audio)

    def test_normalize_task_states_and_progress(self):
        payload = {"data": {"state": "first_success", "progress": 64,
                            "response": {"audio_url": "https://cdn.example/partial.mp3"}}}
        result = music_studio.normalize_task(payload)
        self.assertEqual(result["status"], "generating")
        self.assertEqual(result["progress"], 64)
        self.assertEqual(len(result["tracks"]), 1)

        completed = music_studio.normalize_task({"data": {"status": "completed", "resultUrls": ["https://cdn.example/done.mp3"]}})
        self.assertEqual(completed["status"], "success")
        failed = music_studio.normalize_task({"data": {"status": "failed", "failMsg": "private-key invalid"}}, "private-key")
        self.assertNotIn("private-key", failed["message"])

    def test_ollama_song_draft_parses_and_clears_instrumental_lyrics(self):
        reply = json.dumps({"title": "Hujan di Kota", "lyrics": "[Verse]\\nLangit turun", "style": "Pop ballad, piano"})
        with patch.object(ollama_cloud, "_chat", return_value=reply) as chat:
            draft = ollama_cloud.generate_song_draft(
                "key", "gpt-oss:20b", "Rindu rumah", instrumental=True, category="Ambient & Cinematic"
            )
        self.assertIn('"category": "Ambient & Cinematic"', chat.call_args.args[3])
        self.assertEqual(draft["title"], "Hujan di Kota")
        self.assertEqual(draft["lyrics"], "")
        self.assertEqual(draft["style"], "Pop ballad, piano")

    def test_validate_music_payload_limits_and_instrumental(self):
        body = {"model": "ai-music-api/generate", "input": {
            "custom_mode": True, "instrumental": True, "model": "V6", "title": "Night Rain",
            "style": "Ambient piano and strings", "prompt": "Ambient piano and strings",
        }}
        result = music_studio.validate_request(body)
        self.assertTrue(result["input"]["instrumental"])
        with self.assertRaises(ValueError):
            music_studio.validate_request({"model": "ai-music-api/generate", "input": {
                "instrumental": False, "model": "V6", "title": "No lyric", "style": "Pop",
            }})


if __name__ == "__main__":
    unittest.main()
