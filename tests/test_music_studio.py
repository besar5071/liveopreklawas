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


class MusicStudioTests(unittest.TestCase):
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
        with patch.object(ollama_cloud, "_chat", return_value=reply):
            draft = ollama_cloud.generate_song_draft("key", "gpt-oss:20b", "Rindu rumah", instrumental=True)
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
