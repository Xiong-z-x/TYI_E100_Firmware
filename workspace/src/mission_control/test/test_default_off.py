#!/usr/bin/env python3
"""Static deployment invariants for the mission-control safety boundary."""

from pathlib import Path
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]


class DefaultOffTest(unittest.TestCase):
    def test_only_explicit_wrapper_can_start_test_flight(self) -> None:
        wrapper = REPO_ROOT / "scripts" / "mission"
        self.assertTrue(wrapper.is_file(), "scripts/mission is required")
        text = wrapper.read_text(encoding="utf-8")
        self.assertIn("test_flight --confirm-uav-051", text)
        self.assertIn("run_mission check false false", text)
        self.assertIn("run_mission test_flight true true", text)
        self.assertIn("mission:=${mission}", text)
        self.assertIn("auto_start:=${auto_start}", text)
        self.assertIn("confirm_uav_051:=${confirm}", text)

    def test_runtime_stack_does_not_autostart_mission(self) -> None:
        checked = [
            REPO_ROOT / "docker-compose.yml",
            REPO_ROOT / "docker" / "runtime" / "entrypoint.sh",
        ]
        for path in checked:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("mission:=test_flight", text, str(path))
            self.assertNotIn("mission_main", text, str(path))

    def test_legacy_boot_service_and_force_disarm_are_absent(self) -> None:
        source_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in PACKAGE_ROOT.rglob("*")
            if path.is_file()
            and path.suffix in {".cpp", ".h", ".launch", ".yaml", ".md"}
        )
        self.assertNotIn("forceDisarm(", source_text)
        self.assertNotIn("21196", source_text)
        self.assertNotIn("mission_control_node", source_text)
        self.assertFalse((PACKAGE_ROOT / "launch" / "control.launch").exists())


if __name__ == "__main__":
    unittest.main()
