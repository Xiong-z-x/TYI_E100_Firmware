import json
import threading
import unittest
import urllib.error
import urllib.request

from camera.orin_camera_stream import CameraHttpServer, FrameBuffer


JPEG = b"\xff\xd8test-jpeg-frame\xff\xd9"


class MutableClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FrameBufferTests(unittest.TestCase):
    def test_publish_advances_sequence_and_returns_latest_frame(self) -> None:
        clock = MutableClock()
        frames = FrameBuffer(max_age_sec=2.0, clock=clock)

        frames.publish(JPEG)

        snapshot = frames.snapshot()
        self.assertEqual(snapshot.sequence, 1)
        self.assertEqual(snapshot.frame, JPEG)
        self.assertEqual(snapshot.age_sec, 0.0)
        self.assertTrue(snapshot.fresh)

    def test_frame_becomes_unhealthy_after_max_age(self) -> None:
        clock = MutableClock()
        frames = FrameBuffer(max_age_sec=2.0, clock=clock)
        frames.publish(JPEG)

        clock.value += 2.1

        snapshot = frames.snapshot()
        self.assertFalse(snapshot.fresh)
        self.assertAlmostEqual(snapshot.age_sec, 2.1)


class CameraHttpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.frames = FrameBuffer(max_age_sec=2.0, clock=self.clock)
        self.server = CameraHttpServer(
            ("127.0.0.1", 0),
            self.frames,
            stream_fps=100,
            camera_info={
                "device": "/dev/camera-test",
                "width": 1280,
                "height": 720,
                "capture_fps": 60,
                "stream_fps": 15,
            },
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_health_is_503_until_a_fresh_frame_exists(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{self.base_url}/healthz", timeout=2)

        self.assertEqual(caught.exception.code, 503)
        payload = json.loads(caught.exception.read())
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["sequence"], 0)

    def test_health_and_snapshot_return_the_latest_fresh_frame(self) -> None:
        self.frames.publish(JPEG)

        with urllib.request.urlopen(f"{self.base_url}/healthz", timeout=2) as response:
            payload = json.load(response)
        with urllib.request.urlopen(f"{self.base_url}/snapshot.jpg", timeout=2) as response:
            snapshot = response.read()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["sequence"], 1)
        self.assertEqual(payload["device"], "/dev/camera-test")
        self.assertEqual(snapshot, JPEG)

    def test_stream_is_multipart_and_contains_the_latest_frame(self) -> None:
        self.frames.publish(JPEG)

        with urllib.request.urlopen(f"{self.base_url}/stream.mjpg", timeout=2) as response:
            boundary = response.readline()
            content_type = response.readline()
            content_length = response.readline()
            blank = response.readline()
            frame = response.read(len(JPEG))

        self.assertEqual(boundary, b"--frame\r\n")
        self.assertEqual(content_type, b"Content-Type: image/jpeg\r\n")
        self.assertEqual(
            content_length,
            f"Content-Length: {len(JPEG)}\r\n".encode("ascii"),
        )
        self.assertEqual(blank, b"\r\n")
        self.assertEqual(frame, JPEG)


if __name__ == "__main__":
    unittest.main()
