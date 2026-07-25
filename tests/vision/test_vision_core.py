import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CORE_PATH = ROOT / "docker" / "vision-gateway" / "vision_core.py"
SPEC = importlib.util.spec_from_file_location("vision_core", CORE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

AnimalTracker = MODULE.AnimalTracker
Detection = MODULE.Detection
box_iou = MODULE.box_iou
parse_classes = MODULE.parse_classes


class BoxTests(unittest.TestCase):
    def test_iou(self) -> None:
        self.assertAlmostEqual(box_iou((0, 0, 10, 10), (5, 5, 15, 15)), 25 / 175)
        self.assertEqual(box_iou((0, 0, 1, 1), (2, 2, 3, 3)), 0.0)


class TrackerTests(unittest.TestCase):
    def detection(self, x: float, label: str = "tiger", class_id: int = 1) -> object:
        return Detection(class_id, label, 0.9, (x, 10.0, x + 20.0, 30.0))

    def test_emits_once_after_confirmation(self) -> None:
        tracker = AnimalTracker(confirm_hits=3, max_missed=2)
        self.assertEqual(tracker.update([self.detection(10)], 1.0).events, [])
        self.assertEqual(tracker.update([self.detection(12)], 2.0).events, [])
        update = tracker.update([self.detection(14)], 3.0)
        self.assertEqual(len(update.events), 1)
        self.assertEqual(update.events[0].label, "tiger")
        self.assertEqual(tracker.update([self.detection(16)], 4.0).events, [])

    def test_does_not_cross_match_classes(self) -> None:
        tracker = AnimalTracker(confirm_hits=1)
        first = tracker.update([self.detection(10, "tiger", 1)], 1.0)
        second = tracker.update([self.detection(10, "wolf", 2)], 2.0)
        self.assertNotEqual(first.tracks[0].track_id, second.tracks[0].track_id)

    def test_expires_lost_track(self) -> None:
        tracker = AnimalTracker(confirm_hits=1, max_missed=1)
        first = tracker.update([self.detection(10)], 1.0)
        tracker.update([], 2.0)
        tracker.update([], 3.0)
        second = tracker.update([self.detection(10)], 4.0)
        self.assertNotEqual(first.tracks[0].track_id, second.tracks[0].track_id)


class ConfigTests(unittest.TestCase):
    def test_parse_classes(self) -> None:
        self.assertEqual(
            parse_classes("elephant, tiger,wolf,monkey,peacock"),
            ["elephant", "tiger", "wolf", "monkey", "peacock"],
        )
        with self.assertRaises(ValueError):
            parse_classes("tiger,tiger")


if __name__ == "__main__":
    unittest.main()
