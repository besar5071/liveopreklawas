import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import services


class FakeProcess:
    def __init__(self, data):
        self.stdout = io.BytesIO(data)
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


class LiveAudioSourceTests(unittest.TestCase):
    def test_audio_source_accepts_current_and_legacy_original_video_selections(self):
        self.assertEqual(services._live_audio_source({"audio_source": "video"}), "video")
        self.assertEqual(services._live_audio_source({"audio_source": "Audio asli bawaan video"}), "video")
        self.assertEqual(services._live_audio_source({"a_rand": [], "a_loop": []}), "video")
        self.assertEqual(services._live_audio_source({"audio_source": "library", "a_rand": []}), "video")
        self.assertEqual(services._live_audio_source({"audio_source": "library", "a_rand": [Path("track.mp3")]}), "library")

    def test_empty_library_playlist_is_routed_to_embedded_audio(self):
        manager = services.StreamManager()
        path = Path("video-with-audio.mp4")
        media_info = {"ok": True, "v": {"w": 1280, "h": 720, "fps": 30},
                      "a": {"codec": "aac"}, "dur": 4.0}
        manager._session = lambda *args: "stop"
        manager._prep_worker = lambda _cfg: None
        cfg = {"videos": [path], "v_rand": [path], "audio_source": "library",
               "a_rand": [], "a_loop": [], "audios": [], "vmode": "copy"}
        with (patch.object(services, "media_info", return_value=media_info),
              patch.object(services, "video_copy_ok", return_value=(True, ""))):
            manager._run(cfg)
        self.assertEqual(cfg["audio_source"], "video")
        self.assertEqual(manager.state, "idle")
        self.assertTrue(any("Audio asli memakai stream audio pertama" in note for note in manager.notes))

    def test_original_audio_mode_rejects_a_random_video_playlist_without_audio(self):
        manager = services.StreamManager()
        path = Path("silent-video.mp4")
        media_info = {"ok": True, "v": {"width": 1280}, "a": False, "dur": 4.0}
        with patch.object(services, "media_info", return_value=media_info):
            manager._run({"videos": [path], "v_rand": [path], "audio_source": "video", "a_rand": []})
        self.assertEqual(manager.state, "error")
        self.assertIn("tidak ada video acak yang memiliki audio bawaan", manager.detail)

    def test_video_audio_feeder_extracts_audio_pcm_and_marks_the_current_video(self):
        manager = services.StreamManager()
        session = services._Session()
        audio_path = Path("library-video.mp4")
        pcm = b"\x00" * services.PCM_SEC
        process = FakeProcess(pcm)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(services, "LIVE_DIR", Path(tmp)), \
             patch.object(services.subprocess, "Popen", return_value=process) as popen:
            manager._video_audio_feeder(session, audio_path, 3.5, threading.Event())

        command = popen.call_args.args[0]
        self.assertIn("-re", command)
        self.assertIn("0:a:0", command)
        self.assertIn("3.500", command)
        first = session.aq.get_nowait()
        self.assertEqual(first, ("video_audio", audio_path.name, ""))
        self.assertEqual(session.aq.get_nowait() + session.aq.get_nowait(), pcm)
        self.assertEqual(session.procs, [])


if __name__ == "__main__":
    unittest.main()
