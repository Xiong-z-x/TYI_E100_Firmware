import importlib.util
import json
import sys
import unittest
from http import HTTPStatus
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "docker" / "vision-gateway"
sys.path.insert(0, str(SOURCE_DIR))
SPEC = importlib.util.spec_from_file_location("vision_gateway_app", SOURCE_DIR / "app.py")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

Settings = MODULE.Settings
SharedState = MODULE.SharedState


def settings() -> object:
    config_path = ROOT / "configs" / "vision-gateway" / "config.json"
    return Settings.from_config(json.loads(config_path.read_text(encoding="utf-8")))


class SharedStateTests(unittest.TestCase):
    def test_health_requires_model_and_fresh_source(self) -> None:
        state = SharedState(settings())
        status, payload = state.health()
        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(payload["status"], "unavailable")

        state.note_model_ready()
        state.publish(
            payload={"activeClassCounts": {"tiger": 1}},
            jpeg=b"jpeg",
            events=[],
            inference_ms=42.0,
            loop_fps=12.5,
        )
        status, payload = state.health()
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(payload["model"]["backend"], "engine")
        self.assertEqual(payload["performance"]["framesOk"], 1)

    def test_confirmed_event_updates_session_count(self) -> None:
        state = SharedState(settings())
        state.note_model_ready()
        state.publish(
            payload={"activeClassCounts": {"peacock": 1}},
            jpeg=b"jpeg",
            events=[
                {
                    "event": "animal_confirmed",
                    "label": "peacock",
                    "trackId": 7,
                }
            ],
            inference_ms=30.0,
            loop_fps=15.0,
        )
        counts = state.counts()
        self.assertEqual(counts["sessionConfirmedTrackEvents"]["peacock"], 1)
        self.assertEqual(counts["activeConfirmedTracks"]["peacock"], 1)

    def test_source_failure_marks_health_unavailable(self) -> None:
        state = SharedState(settings())
        state.note_model_ready()
        state.publish({}, b"jpeg", [], 30.0, 15.0)
        state.note_source_error("camera timeout")
        status, payload = state.health()
        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(payload["source"]["error"], "camera timeout")


if __name__ == "__main__":
    unittest.main()
