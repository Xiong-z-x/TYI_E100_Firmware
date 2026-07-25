import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class RepositoryWiringTests(unittest.TestCase):
    def test_vision_service_uses_gpu_but_never_owns_camera_device(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        block = compose.split("  vision-gateway:", 1)[1]
        self.assertIn("runtime: nvidia", block)
        self.assertIn("http://127.0.0.1:8090/snapshot.jpg", block)
        self.assertIn("./models/vision:/opt/uav/models:ro", block)
        self.assertNotIn("/dev:/dev", block)
        self.assertNotIn("/dev/video", block)

    def test_five_contest_classes_are_configured_in_required_order(self) -> None:
        path = ROOT / "configs" / "vision-gateway" / "animal_visual_prompts.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["classes"],
            ["elephant", "tiger", "wolf", "monkey", "peacock"],
        )
        self.assertEqual(len(payload["boxes"]), 15)
        self.assertEqual({item["classId"] for item in payload["boxes"]}, set(range(5)))

    def test_runtime_source_does_not_reference_video_devices(self) -> None:
        source = (ROOT / "docker" / "vision-gateway" / "app.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("/dev/video", source)
        self.assertIn("/snapshot.jpg", source)

    def test_model_artifacts_are_ignored(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("models/vision/*", gitignore)
        self.assertIn("!models/vision/.gitkeep", gitignore)


if __name__ == "__main__":
    unittest.main()
