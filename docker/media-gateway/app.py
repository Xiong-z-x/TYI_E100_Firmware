#!/usr/bin/env python3
import json
import os
import secrets
import shlex
import socket
import subprocess
import fcntl
import struct
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[media-gateway] {stamp} {message}", flush=True)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_request_json(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length == 0:
        return {}
    body = handler.rfile.read(length)
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


def probe_json(url: str, timeout_sec: float = 2.0) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def run_gst_command(command: str, timeout_sec: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-lc", command],
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )


def probe_http(url: str, timeout_sec: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as response:
            response.read(1)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def profile_name(profile: Dict[str, Any]) -> str:
    width = int(profile["width"])
    height = int(profile["height"])
    fps = int(profile["fps"])
    known = {
        (1280, 720, 60): "720p60",
        (1280, 720, 30): "720p30",
        (854, 480, 30): "480p30",
        (640, 480, 30): "480p30",
        (640, 480, 15): "480p15",
        (640, 480, 10): "480p10",
        (1920, 1080, 30): "1080p30",
    }
    return known.get((width, height, fps), f"{width}x{height}_{fps}")


def session_bitrate_kbps(name: str) -> int:
    mapping = {
        "1080p30": 6000,
        "720p60": 5000,
        "720p30": 3000,
        "480p30": 1500,
        "480p15": 1200,
        "480p10": 900,
    }
    return mapping.get(name, 3000)


def media_encoder_mode() -> str:
    return os.environ.get("MEDIA_GATEWAY_ENCODER", "auto").strip().lower() or "auto"


def h264_encode_pipeline(width: int, height: int, fps: int, bitrate_kbps: int) -> str:
    key_int = max(1, fps * 2)
    mode = media_encoder_mode()
    software = (
        f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} "
        f"key-int-max={key_int} byte-stream=true aud=true threads=2 ! "
        "h264parse config-interval=-1"
    )
    hardware = (
        "video/x-raw,format=NV12 ! nvvidconv ! "
        f'"video/x-raw(memory:NVMM),format=NV12,width={width},height={height},framerate={fps}/1" ! '
        f"nvv4l2h264enc insert-sps-pps=true bitrate={bitrate_kbps * 1000} "
        f"iframeinterval={key_int} control-rate=1 maxperf-enable=true ! "
        "h264parse config-interval=-1"
    )
    if mode in {"x264", "software", "sw"}:
        return software
    if mode in {"nvv4l2", "hardware", "hw"}:
        return hardware
    if mode == "auto":
        return hardware
    log(f"unknown MEDIA_GATEWAY_ENCODER={mode!r}; using software encoder")
    return software


def resolve_capture_device(device: str) -> str:
    if not device:
        return device
    try:
        path = Path(device)
        if not path.exists():
            return device
        resolved = str(path.resolve())
        if resolved.startswith('/dev/video'):
            return resolved
    except Exception:
        return device
    return device


def probe_device(device: str, profiles: List[Dict[str, Any]], timeout_sec: int) -> Dict[str, Any]:
    exists = Path(device).exists()
    resolved_device = resolve_capture_device(device)
    result: Dict[str, Any] = {
        "device": device,
        "resolvedDevice": resolved_device,
        "exists": exists,
        "captureReady": False,
        "busy": False,
        "selectedProfile": None,
        "supportedProfiles": [],
        "error": None,
    }
    if not exists:
        result["error"] = "device path not found"
        return result

    base_probe = run_gst_command(
        f"timeout {timeout_sec} gst-launch-1.0 -q v4l2src device={shlex.quote(resolved_device)} num-buffers=1 ! fakesink",
        timeout_sec + 2,
    )
    if base_probe.returncode != 0:
        error_text = (base_probe.stderr or base_probe.stdout).strip() or "device is not a capture source"
        lowered = error_text.lower()
        if "busy" in lowered or "resource busy" in lowered:
            result["busy"] = True
        result["error"] = error_text
        return result

    result["captureReady"] = True
    for profile in profiles:
        caps = (
            f"{profile['format']},width={profile['width']},height={profile['height']},"
            f"framerate={profile['fps']}/1"
        )
        command = (
            f"timeout {timeout_sec} gst-launch-1.0 -q "
            f"v4l2src device={shlex.quote(resolved_device)} num-buffers=1 ! "
            f"{caps} ! fakesink"
        )
        attempt = run_gst_command(command, timeout_sec + 2)
        if attempt.returncode == 0:
            result["supportedProfiles"].append(profile)

    if result["supportedProfiles"]:
        result["selectedProfile"] = result["supportedProfiles"][0]
    return result


def probe_camera_candidates(config: Dict[str, Any]) -> Dict[str, Any]:
    camera = config["camera"]
    profiles = camera["preferredProfiles"]
    timeout_sec = int(config["validation"].get("startupProbeTimeoutSec", 5))
    source_mode = str(camera.get("sourceMode", "v4l2")).strip().lower()

    if source_mode in {"mjpeg", "http_mjpeg", "realsense_mjpeg"}:
        source_url = str(camera.get("sourceUrl", "http://127.0.0.1:8765/v1/mjpeg"))
        ready = probe_http(source_url, timeout_sec=float(timeout_sec))
        selected_profile = profiles[0] if profiles else None
        probe_result = {
            "device": source_url,
            "resolvedDevice": source_url,
            "exists": True,
            "captureReady": ready,
            "busy": False,
            "selectedProfile": selected_profile if ready else None,
            "supportedProfiles": profiles if ready else [],
            "error": None if ready else "MJPEG source is not ready",
        }
        return {
            "probeResults": [probe_result],
            "selectedDevice": source_url if ready and selected_profile is not None else None,
            "selectedProfile": selected_profile if ready else None,
            "availableProfiles": list(profiles) if ready and selected_profile is not None else [],
            "lastError": None if ready else "MJPEG source is not ready",
        }

    candidates = [camera["preferredDevice"], *camera.get("fallbackDevices", [])]
    probe_results: List[Dict[str, Any]] = []
    selected_device = None
    selected_profile = None
    available_profiles: List[Dict[str, Any]] = []
    for device in candidates:
        result = probe_device(device, profiles, timeout_sec)
        probe_results.append(result)
        if selected_device is None and result["captureReady"]:
            selected_device = device
            selected_profile = result["selectedProfile"] or profiles[0]
            available_profiles = list(result["supportedProfiles"]) or [selected_profile]

    last_error = None
    if selected_device is None:
        last_error = "no capture-capable camera device matched the configured candidates"

    return {
        "probeResults": probe_results,
        "selectedDevice": selected_device,
        "selectedProfile": selected_profile,
        "availableProfiles": available_profiles,
        "lastError": last_error,
    }


def selected_resolved_device(probe_results: List[Dict[str, Any]], selected_device: Optional[str]) -> Optional[str]:
    if not selected_device:
        return None
    for result in probe_results:
        if result.get("device") == selected_device:
            resolved = result.get("resolvedDevice")
            if resolved:
                return str(resolved)
    resolved = resolve_capture_device(selected_device)
    return resolved or None


def yaml_single_quote(value: str) -> str:
    return value.replace("'", "''")


def resolve_interface_ipv4(name: str) -> Optional[str]:
    if not name:
        return None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        request = struct.pack("256s", name.encode("utf-8")[:15])
        response = fcntl.ioctl(sock.fileno(), 0x8915, request)
        return socket.inet_ntoa(response[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def resolve_interface_ipv4s(names: List[str]) -> List[str]:
    resolved: List[str] = []
    seen: set[str] = set()
    for name in names:
        address = resolve_interface_ipv4(str(name))
        if not address or address in seen:
            continue
        resolved.append(address)
        seen.add(address)
    return resolved


@dataclass
class MediaSession:
    session_id: str
    profile: Dict[str, Any]
    path_name: str
    created_at: float = field(default_factory=time.time)
    state: str = "ready"
    encoder: str = "x264"
    signaling_mode: str = "whep"

    def snapshot(self, ports: Dict[str, int], device: Optional[str]) -> Dict[str, Any]:
        viewer_path = f"/{self.path_name}/"
        whep_path = f"/{self.path_name}/whep"
        return {
            "sessionId": self.session_id,
            "profile": profile_name(self.profile),
            "encoder": self.encoder,
            "state": self.state,
            "selectedDevice": device,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.created_at)),
            "stream": {
                "signalingMode": self.signaling_mode,
                "path": self.path_name,
                "viewerPath": viewer_path,
                "whepPath": whep_path,
                "rtspPath": self.path_name,
                "webrtcPort": ports["webrtc"],
                "rtspPort": ports["rtsp"],
            },
            "viewerUrl": f"http://127.0.0.1:{ports['webrtc']}{viewer_path}",
            "whepUrl": f"http://127.0.0.1:{ports['webrtc']}{whep_path}",
            "rtspUrl": f"rtsp://127.0.0.1:{ports['rtsp']}/{self.path_name}",
        }


class MediaState:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.selected_device: Optional[str] = None
        self.selected_profile: Optional[Dict[str, Any]] = None
        self.available_profiles: List[Dict[str, Any]] = []
        self.probe_results: List[Dict[str, Any]] = []
        self.last_error: Optional[str] = None
        self.mediamtx_ready = False
        self.mediamtx_pid: Optional[int] = None
        self.path_statuses: List[Dict[str, Any]] = []
        self.sessions: Dict[str, MediaSession] = {}

    def ports(self) -> Dict[str, int]:
        mediamtx = self.config.get("mediamtx", {})
        return {
            "api": int(mediamtx.get("apiPort", 9997)),
            "rtsp": int(mediamtx.get("rtspPort", 8554)),
            "webrtc": int(mediamtx.get("webrtcPort", 8889)),
        }

    def path_for_profile(self, profile: Dict[str, Any]) -> str:
        prefix = self.config.get("mediamtx", {}).get("pathPrefix", "camera")
        return f"{prefix}-{profile_name(profile)}"

    def snapshot(self) -> Dict[str, Any]:
        ports = self.ports()
        with self.lock:
            return {
                "ok": self.selected_device is not None and self.mediamtx_ready,
                "selectedDevice": self.selected_device,
                "selectedProfile": self.selected_profile,
                "availableProfiles": [profile_name(profile) for profile in self.available_profiles],
                "onlineProfiles": [
                    item["name"]
                    for item in self.path_statuses
                    if item.get("ready") and item.get("online")
                ],
                "streamStats": {
                    "pathCount": len(self.path_statuses),
                    "readyCount": sum(1 for item in self.path_statuses if item.get("ready")),
                    "onlineCount": sum(1 for item in self.path_statuses if item.get("online")),
                },
                "paths": list(self.path_statuses),
                "probeResults": list(self.probe_results),
                "lastError": self.last_error,
                "sessionCount": len(self.sessions),
                "mediamtx": {
                    "ready": self.mediamtx_ready,
                    "pid": self.mediamtx_pid,
                    "apiPort": ports["api"],
                    "rtspPort": ports["rtsp"],
                    "webrtcPort": ports["webrtc"],
                },
            }

    def get_profile(self, requested_name: Optional[str]) -> Dict[str, Any]:
        with self.lock:
            if requested_name:
                for profile in self.available_profiles:
                    if profile_name(profile) == requested_name:
                        return dict(profile)
                fallback = self.selected_profile or (self.available_profiles[0] if self.available_profiles else None)
                if fallback is not None:
                    log(
                        f"requested unsupported camera profile {requested_name}; "
                        f"falling back to {profile_name(fallback)}"
                    )
                    return dict(fallback)
                raise KeyError(requested_name)
            if self.selected_profile:
                return dict(self.selected_profile)
            if self.available_profiles:
                return dict(self.available_profiles[0])
            raise RuntimeError("no validated camera profile is available")

    def add_session(self, session: MediaSession) -> None:
        with self.lock:
            self.sessions[session.session_id] = session

    def get_session(self, session_id: str) -> Optional[MediaSession]:
        with self.lock:
            return self.sessions.get(session_id)

    def remove_session(self, session_id: str) -> Optional[MediaSession]:
        with self.lock:
            return self.sessions.pop(session_id, None)

    def list_sessions(self) -> List[Dict[str, Any]]:
        ports = self.ports()
        with self.lock:
            return [
                session.snapshot(ports, self.selected_device)
                for session in self.sessions.values()
            ]


class MediaMtxController:
    def __init__(self, state: MediaState) -> None:
        self.state = state
        self.process: Optional[subprocess.Popen] = None
        self.log_thread: Optional[threading.Thread] = None
        self._offline_polls = 0
        self._missing_polls = 0
        self._last_restart_monotonic = 0.0

    def start(self) -> None:
        if self.state.selected_device is None or not self.state.available_profiles:
            return

        self.stop()
        mediamtx = self.state.config.get("mediamtx", {})
        config_path = mediamtx.get("generatedConfigPath", "/tmp/mediamtx.generated.yml")
        binary_path = os.environ.get("MEDIAMTX_BIN", mediamtx.get("binaryPath", "/usr/local/bin/mediamtx"))
        Path(config_path).write_text(self._build_config(), encoding="utf-8")
        log(f"starting mediamtx with config {config_path}")
        self.process = subprocess.Popen(
            [binary_path, config_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.log_thread = threading.Thread(target=self._relay_logs, daemon=True)
        self.log_thread.start()

        deadline = time.time() + 15
        api_port = self.state.ports()["api"]
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"mediamtx exited early with code {self.process.returncode}")
            if probe_http(f"http://127.0.0.1:{api_port}/", timeout_sec=1.0):
                with self.state.lock:
                    self.state.mediamtx_ready = True
                    self.state.mediamtx_pid = self.process.pid
                    if self.state.last_error == "mediamtx is not reachable":
                        self.state.last_error = None
                return
            time.sleep(0.5)

        raise TimeoutError("timed out waiting for mediamtx HTTP API")

    def stop(self) -> None:
        self._offline_polls = 0
        self._missing_polls = 0
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        with self.state.lock:
            self.state.mediamtx_ready = False
            self.state.mediamtx_pid = None

    def refresh_health(self) -> None:
        api_port = self.state.ports()["api"]
        ready = probe_http(f"http://127.0.0.1:{api_port}/", timeout_sec=1.0)
        path_payload = probe_json(f"http://127.0.0.1:{api_port}/v3/paths/list", timeout_sec=2.0) or {}
        items = []
        for item in path_payload.get("items", []):
            items.append(
                {
                    "name": item.get("name"),
                    "ready": bool(item.get("ready")),
                    "online": bool(item.get("online")),
                    "tracks": item.get("tracks", []),
                    "bytesReceived": item.get("bytesReceived", 0),
                    "bytesSent": item.get("bytesSent", 0),
                    "readers": len(item.get("readers", [])),
                }
            )
        with self.state.lock:
            self.state.mediamtx_ready = ready and self.process is not None and self.process.poll() is None
            self.state.path_statuses = items
            if not self.state.mediamtx_ready and self.state.selected_device:
                self.state.last_error = "mediamtx is not reachable"

    def reconcile_camera(self) -> None:
        with self.state.lock:
            previous_selected = self.state.selected_device
            previous_profile = self.state.selected_profile
            previous_available_profiles = list(self.state.available_profiles)
            online_now = any(item.get("ready") and item.get("online") for item in self.state.path_statuses)
            publisher_ready = self.state.mediamtx_ready
            publisher_mode = str(
                self.state.config.get("mediamtx", {}).get("publisherMode", "always")
            ).strip().lower()

        publisher_running = self.process is not None and self.process.poll() is None
        on_demand_mode = publisher_mode != "always"
        validation = self.state.config.get("validation", {})
        offline_recover_polls = max(1, int(validation.get("offlineRecoverPolls", 2)))
        missing_recover_polls = max(1, int(validation.get("missingRecoverPolls", 3)))

        # In on-demand mode an idle path is expected until a WHEP/RTSP reader connects.
        if previous_selected and publisher_running and publisher_ready and (online_now or on_demand_mode):
            source_mode = str(self.state.config.get("camera", {}).get("sourceMode", "v4l2")).strip().lower()
            if source_mode in {"mjpeg", "http_mjpeg", "realsense_mjpeg"}:
                with self.state.lock:
                    refreshed_probe_results = []
                    for result in self.state.probe_results:
                        if result.get("device") == previous_selected:
                            updated = dict(result)
                            updated["captureReady"] = True
                            updated["selectedProfile"] = previous_profile
                            updated["supportedProfiles"] = previous_available_profiles
                            updated["error"] = None
                            refreshed_probe_results.append(updated)
                        else:
                            refreshed_probe_results.append(result)
                    self.state.probe_results = refreshed_probe_results
                    if self.state.last_error == "MJPEG source is not ready":
                        self.state.last_error = None
            self._offline_polls = 0
            self._missing_polls = 0
            return

        probe_state = probe_camera_candidates(self.state.config)
        preferred_device = str(self.state.config["camera"].get("preferredDevice", ""))
        preferred_probe = None
        for result in probe_state["probeResults"]:
            if result.get("device") == preferred_device:
                preferred_probe = result
                break

        if publisher_running and probe_state["selectedDevice"] is None:
            busy_probe = None
            if previous_selected:
                for result in probe_state["probeResults"]:
                    if result.get("device") == previous_selected:
                        busy_probe = result
                        break
            if busy_probe is None and preferred_probe and preferred_probe.get("busy"):
                busy_probe = preferred_probe
            if busy_probe and busy_probe.get("busy"):
                selected_device = previous_selected or preferred_device or busy_probe.get("device")
                selected_profile = previous_profile or (previous_available_profiles[0] if previous_available_profiles else None)
                if selected_profile is None:
                    preferred_profiles = self.state.config["camera"].get("preferredProfiles", [])
                    if preferred_profiles:
                        selected_profile = preferred_profiles[0]
                probe_state["selectedDevice"] = selected_device
                probe_state["selectedProfile"] = selected_profile
                probe_state["availableProfiles"] = previous_available_profiles or ([selected_profile] if selected_profile else [])
                probe_state["lastError"] = None

        with self.state.lock:
            self.state.probe_results = probe_state["probeResults"]
            self.state.selected_device = probe_state["selectedDevice"]
            self.state.selected_profile = probe_state["selectedProfile"]
            self.state.available_profiles = probe_state["availableProfiles"]
            if probe_state["selectedDevice"] is None:
                self.state.last_error = probe_state["lastError"]
            elif self.state.last_error == probe_state["lastError"]:
                self.state.last_error = None
            current_selected = self.state.selected_device

        reason = None
        if current_selected is None:
            self._offline_polls = 0
            if publisher_running:
                self._missing_polls += 1
                if self._missing_polls >= missing_recover_polls:
                    log("camera path is temporarily missing; keeping mediamtx running for by-path auto-recovery")
                return
            self._missing_polls = 0
        else:
            self._missing_polls = 0
            if previous_selected and previous_selected != current_selected:
                reason = f"camera device changed: {previous_selected} -> {current_selected}"

        if reason is None:
            if not publisher_running:
                self._offline_polls = 0
                reason = "media publisher is not running"
            elif not publisher_ready:
                self._offline_polls += 1
                if self._offline_polls < offline_recover_polls:
                    return
                reason = "mediamtx process is not ready"
            elif not online_now and not on_demand_mode:
                self._offline_polls += 1
                if self._offline_polls < offline_recover_polls:
                    return
                reason = "camera stream is offline while capture probe is healthy"
            else:
                self._offline_polls = 0
                return

        self._offline_polls = 0
        now = time.monotonic()
        if now - self._last_restart_monotonic < 8.0:
            return
        self._last_restart_monotonic = now
        log(f"reconciling media pipeline: {reason}")
        self.stop()
        if current_selected and probe_state["availableProfiles"]:
            try:
                self.start()
            except Exception as exc:  # noqa: BLE001
                with self.state.lock:
                    self.state.last_error = str(exc)
                log(f"failed to restart mediamtx after reconcile: {exc}")

    def _relay_logs(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        for line in self.process.stdout:
            text = line.rstrip()
            if text:
                print(f"[mediamtx] {text}", flush=True)

    def _build_config(self) -> str:
        mediamtx = self.state.config.get("mediamtx", {})
        api_port = int(mediamtx.get("apiPort", 9997))
        rtsp_port = int(mediamtx.get("rtspPort", 8554))
        webrtc_port = int(mediamtx.get("webrtcPort", 8889))
        webrtc_ice_udp_port = int(mediamtx.get("webrtcIceUDPPort", 8189))
        log_level = mediamtx.get("logLevel", "info")
        publisher_mode = str(mediamtx.get("publisherMode", "always")).lower()
        on_demand_timeout_sec = int(mediamtx.get("onDemandStartTimeoutSec", 20))
        on_demand_close_sec = int(mediamtx.get("onDemandCloseAfterSec", 10))
        enable_rtmp = bool(mediamtx.get("rtmpEnabled", False))
        enable_hls = bool(mediamtx.get("hlsEnabled", False))
        enable_srt = bool(mediamtx.get("srtEnabled", False))

        lines = [
            f"logLevel: {log_level}",
            "api: yes",
            f"apiAddress: 127.0.0.1:{api_port}",
            "rtsp: yes",
            f"rtspAddress: :{rtsp_port}",
            f"rtmp: {'yes' if enable_rtmp else 'no'}",
            f"hls: {'yes' if enable_hls else 'no'}",
            "webrtc: yes",
            f"webrtcAddress: :{webrtc_port}",
            "webrtcEncryption: no",
            f"srt: {'yes' if enable_srt else 'no'}",
        ]

        interface_list = mediamtx.get("webrtcIPsFromInterfacesList") or []
        resolved_interface_ips = resolve_interface_ipv4s([str(item) for item in interface_list])
        additional_hosts = mediamtx.get("webrtcAdditionalHosts") or []

        if resolved_interface_ips:
            bind_ip = resolved_interface_ips[0]
            lines.append("webrtcIPsFromInterfaces: no")
            lines.append(f"webrtcLocalUDPAddress: {bind_ip}:{webrtc_ice_udp_port}")
            additional_hosts = [*resolved_interface_ips, *additional_hosts]
            log(
                "resolved WebRTC announce interfaces "
                f"{interface_list} -> {resolved_interface_ips}, binding ICE UDP on {bind_ip}:{webrtc_ice_udp_port}"
            )
        else:
            webrtc_ips_from_interfaces = mediamtx.get("webrtcIPsFromInterfaces")
            if webrtc_ips_from_interfaces is not None:
                lines.append(f"webrtcIPsFromInterfaces: {str(bool(webrtc_ips_from_interfaces)).lower()}")
            if interface_list:
                quoted = ", ".join(json.dumps(str(item)) for item in interface_list)
                lines.append(f"webrtcIPsFromInterfacesList: [{quoted}]")
            if interface_list:
                log(f"failed to resolve WebRTC announce interfaces: {interface_list}")

        deduped_hosts: List[str] = []
        seen_hosts: set[str] = set()
        for item in additional_hosts:
            host = str(item)
            if not host or host in seen_hosts:
                continue
            deduped_hosts.append(host)
            seen_hosts.add(host)

        if deduped_hosts:
            quoted = ", ".join(json.dumps(host) for host in deduped_hosts)
            lines.append(f"webrtcAdditionalHosts: [{quoted}]")

        lines.append("paths:")

        for profile in self.state.available_profiles:
            path_name = self.state.path_for_profile(profile)
            command = self._publisher_command(self.state.selected_device or "", profile, path_name, rtsp_port)
            path_lines = [f"  {path_name}:"]
            if publisher_mode == "always":
                path_lines.extend(
                    [
                        f"    runOnInit: '{yaml_single_quote(command)}'",
                        "    runOnInitRestart: yes",
                    ]
                )
            else:
                path_lines.extend(
                    [
                        f"    runOnDemand: '{yaml_single_quote(command)}'",
                        "    runOnDemandRestart: yes",
                        f"    runOnDemandStartTimeout: {on_demand_timeout_sec}s",
                        f"    runOnDemandCloseAfter: {on_demand_close_sec}s",
                    ]
                )
            lines.extend(path_lines)
        return "\n".join(lines) + "\n"

    def _publisher_command(self, device: str, profile: Dict[str, Any], path_name: str, rtsp_port: int) -> str:
        source_mode = str(self.state.config.get("camera", {}).get("sourceMode", "v4l2")).strip().lower()
        publisher_device = device
        resolved_device = resolve_capture_device(device)
        if device.startswith('/dev/v4l/by-path/') and resolved_device.startswith('/dev/video'):
            log(f"using stable capture path {device} for publisher; current node is {resolved_device}")
        elif resolved_device != device:
            publisher_device = resolved_device
            log(f"resolved capture device {device} -> {resolved_device} for publisher")
        width = int(profile["width"])
        height = int(profile["height"])
        fps = int(profile["fps"])
        source_caps = f"{profile['format']},width={width},height={height},framerate={fps}/1"
        bitrate_kbps = session_bitrate_kbps(profile_name(profile))
        encode = h264_encode_pipeline(width, height, fps, bitrate_kbps)
        software_encode = (
            f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} "
            f"key-int-max={max(1, fps * 2)} byte-stream=true aud=true threads=2 ! "
            "h264parse config-interval=-1"
        )
        if source_mode in {"mjpeg", "http_mjpeg", "realsense_mjpeg"}:
            source_url = str(self.state.config.get("camera", {}).get("sourceUrl") or device)
            source = (
                f"souphttpsrc location={shlex.quote(source_url)} is-live=true do-timestamp=true timeout=5 ! "
                "multipartdemux ! image/jpeg,framerate={fps}/1 ! jpegdec ! videoconvert ! videoscale ! "
            ).format(fps=fps)
            scaled_height = int(round((width * 3.0) / 4.0))
            crop_total = max(0, scaled_height - height)
            crop_top = crop_total // 2
            crop_bottom = crop_total - crop_top
            if crop_total > 0:
                source += (
                    f"video/x-raw,width={width},height={scaled_height},framerate={fps}/1 ! "
                    f"videocrop top={crop_top} bottom={crop_bottom} ! "
                )
            source += f"video/x-raw,width={width},height={height},framerate={fps}/1 ! "
            location = f"rtsp://127.0.0.1:{rtsp_port}/{path_name}"
            return (
                "bash -lc "
                + shlex.quote(
                    (
                        "exec gst-launch-1.0 -q " + source + encode
                        + f" ! rtspclientsink protocols=tcp location={shlex.quote(location)}"
                    )
                    if media_encoder_mode() != "auto"
                    else (
                        "gst-launch-1.0 -q " + source + encode
                        + f" ! rtspclientsink protocols=tcp location={shlex.quote(location)}"
                        + " || exec gst-launch-1.0 -q " + source + software_encode
                        + f" ! rtspclientsink protocols=tcp location={shlex.quote(location)}"
                    )
                )
            )

        source = f"v4l2src device={shlex.quote(publisher_device)} do-timestamp=true ! {source_caps} ! "
        if profile["format"] == "image/jpeg":
            source += "jpegdec ! videoconvert ! "
        else:
            source += "videoconvert ! "

        location = f"rtsp://127.0.0.1:{rtsp_port}/{path_name}"
        return (
            "bash -lc "
            + shlex.quote(
                (
                    "exec gst-launch-1.0 -q " + source + encode
                    + f" ! rtspclientsink protocols=tcp location={shlex.quote(location)}"
                )
                if media_encoder_mode() != "auto"
                else (
                    "gst-launch-1.0 -q " + source + encode
                    + f" ! rtspclientsink protocols=tcp location={shlex.quote(location)}"
                    + " || exec gst-launch-1.0 -q " + source + software_encode
                    + f" ! rtspclientsink protocols=tcp location={shlex.quote(location)}"
                )
            )
        )


class SessionManager:
    def __init__(self, state: MediaState) -> None:
        self.state = state
        self.session_timeout_sec = int(self.state.config.get("sessionTimeoutSec", 600))
        self.cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self.cleanup_thread.start()

    def create_session(self, requested_profile: Optional[str]) -> Dict[str, Any]:
        if not self.state.selected_device:
            raise RuntimeError("no capture-capable camera available")
        if not self.state.mediamtx_ready:
            raise RuntimeError("media-gateway is not ready")
        try:
            profile = self.state.get_profile(requested_profile)
        except KeyError as exc:
            raise RuntimeError(f"unsupported camera profile: {exc.args[0]}") from exc

        session = MediaSession(
            session_id=secrets.token_urlsafe(12),
            profile=profile,
            path_name=self.state.path_for_profile(profile),
        )
        self.state.add_session(session)
        log(f"created WHEP session {session.session_id} for {session.path_name}")
        return session.snapshot(self.state.ports(), self.state.selected_device)

    def get_session_snapshot(self, session_id: str) -> Dict[str, Any]:
        session = self.state.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        session.created_at = time.time()
        return session.snapshot(self.state.ports(), self.state.selected_device)

    def close_session(self, session_id: str) -> None:
        self.state.remove_session(session_id)

    def _cleanup_loop(self) -> None:
        while True:
            cutoff = time.time() - self.session_timeout_sec
            with self.state.lock:
                stale = [
                    session_id
                    for session_id, session in self.state.sessions.items()
                    if session.created_at < cutoff
                ]
            for session_id in stale:
                log(f"closing stale session {session_id}")
                self.close_session(session_id)
            time.sleep(10)


class MediaPoller(threading.Thread):
    def __init__(self, controller: MediaMtxController) -> None:
        super().__init__(daemon=True)
        self.controller = controller

    def run(self) -> None:
        while True:
            self.controller.refresh_health()
            self.controller.reconcile_camera()
            time.sleep(5)


def startup_probe(state: MediaState) -> None:
    probe_state = probe_camera_candidates(state.config)
    with state.lock:
        state.probe_results = probe_state["probeResults"]
        state.selected_device = probe_state["selectedDevice"]
        state.selected_profile = probe_state["selectedProfile"]
        state.available_profiles = probe_state["availableProfiles"]
        state.last_error = probe_state["lastError"]


class MediaGatewayHandler(BaseHTTPRequestHandler):
    state: MediaState = None  # type: ignore[assignment]
    sessions: SessionManager = None  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        log(format % args)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path
        if path == "/healthz":
            snapshot = self.state.snapshot()
            status = HTTPStatus.OK if snapshot["ok"] else HTTPStatus.SERVICE_UNAVAILABLE
            json_response(self, status, snapshot)
            return
        if path == "/v1/cameras":
            json_response(self, HTTPStatus.OK, self.state.snapshot())
            return
        if path == "/v1/webrtc/sessions":
            json_response(self, HTTPStatus.OK, {"items": self.state.list_sessions()})
            return
        if path.startswith("/v1/webrtc/sessions/"):
            session_id = path.split("/")[4]
            try:
                json_response(self, HTTPStatus.OK, self.sessions.get_session_snapshot(session_id))
            except KeyError:
                json_response(self, HTTPStatus.NOT_FOUND, {"error": f"unknown session {session_id}"})
            return
        json_response(self, HTTPStatus.NOT_FOUND, {"error": f"unknown path {path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path
        try:
            payload = read_request_json(self)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"invalid JSON payload: {exc}"})
            return
        if path == "/v1/webrtc/sessions":
            snapshot = self.state.snapshot()
            if not snapshot["selectedDevice"]:
                json_response(self, HTTPStatus.SERVICE_UNAVAILABLE, {"error": snapshot["lastError"]})
                return
            if not snapshot["mediamtx"]["ready"]:
                json_response(self, HTTPStatus.SERVICE_UNAVAILABLE, {"error": snapshot["lastError"] or "mediamtx is not ready"})
                return
            try:
                response = self.sessions.create_session(payload.get("profile"))
                json_response(self, HTTPStatus.OK, response)
            except Exception as exc:  # noqa: BLE001
                log(f"failed to create WHEP session: {exc}")
                json_response(self, HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return
        if path.startswith("/v1/webrtc/sessions/") and (path.endswith("/answer") or path.endswith("/ice")):
            json_response(
                self,
                HTTPStatus.GONE,
                {"error": "media-gateway now uses WHEP; direct answer/ICE upload is not required"},
            )
            return
        json_response(self, HTTPStatus.NOT_FOUND, {"error": f"unknown path {path}"})

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path.startswith("/v1/webrtc/sessions/"):
            session_id = self.path.split("/")[4]
            self.sessions.close_session(session_id)
            json_response(self, HTTPStatus.OK, {"closed": True, "sessionId": session_id})
            return
        json_response(self, HTTPStatus.NOT_FOUND, {"error": f"unknown path {self.path}"})


def main() -> None:
    config_path = os.environ.get(
        "MEDIA_GATEWAY_CONFIG_PATH",
        "/opt/uav/configs/media-gateway/config.json",
    )
    config = load_json(config_path)
    config["httpPort"] = int(os.environ.get("MEDIA_GATEWAY_HTTP_PORT", config.get("httpPort", 8555)))
    preferred_device = os.environ.get("MEDIA_CAMERA_PREFERRED_DEVICE")
    if preferred_device:
        config["camera"]["preferredDevice"] = preferred_device

    state = MediaState(config)
    startup_probe(state)
    snapshot = state.snapshot()
    if snapshot["selectedDevice"]:
        log(
            f"selected camera {snapshot['selectedDevice']} "
            f"with profiles {snapshot['availableProfiles']}"
        )
    else:
        log(f"camera probe failed: {snapshot['lastError']}")

    controller = MediaMtxController(state)
    if snapshot["selectedDevice"]:
        try:
            controller.start()
        except Exception as exc:  # noqa: BLE001
            with state.lock:
                state.last_error = str(exc)
            log(f"failed to start mediamtx: {exc}")

    MediaPoller(controller).start()
    session_manager = SessionManager(state)

    MediaGatewayHandler.state = state
    MediaGatewayHandler.sessions = session_manager

    server = ThreadingHTTPServer(("0.0.0.0", config["httpPort"]), MediaGatewayHandler)
    log(f"HTTP API listening on {config['httpPort']}")
    server.serve_forever()


if __name__ == "__main__":
    main()
