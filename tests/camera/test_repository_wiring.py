import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEVICE = (
    "/dev/v4l/by-id/"
    "usb-HD_Camera_Manufacturer_USB_2.0_Camera-video-index0"
)


class RepositoryWiringTests(unittest.TestCase):
    def test_systemd_unit_runs_the_repository_camera_owner(self) -> None:
        unit = (REPO_ROOT / "camera" / "orin-camera-stream.service").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "ExecStart=/usr/bin/python3 "
            "/home/tfboys_nano/TYI_E100_Firmware/camera/orin_camera_stream.py",
            unit,
        )
        self.assertIn(f"--device {DEVICE}", unit)
        self.assertIn("SupplementaryGroups=video", unit)
        self.assertIn("Restart=always", unit)

    def test_media_gateway_consumes_the_owner_mjpeg_stream(self) -> None:
        config = json.loads(
            (REPO_ROOT / "configs" / "media-gateway" / "config.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(config["camera"]["sourceMode"], "mjpeg")
        self.assertEqual(
            config["camera"]["sourceUrl"],
            "http://127.0.0.1:8090/stream.mjpg",
        )

    def test_env_uses_the_current_stable_camera_alias(self) -> None:
        env_text = (REPO_ROOT / ".env").read_text(encoding="utf-8")

        self.assertIn(f"MEDIA_CAMERA_PREFERRED_DEVICE={DEVICE}", env_text)


if __name__ == "__main__":
    unittest.main()
