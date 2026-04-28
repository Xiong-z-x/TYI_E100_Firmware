#!/usr/bin/env python3
import argparse
import ast
import base64
import io
import json
import math
import os
import re
import time
from typing import Any, Dict, List, Tuple
from urllib import error, request

import numpy as np
from PIL import Image, ImageDraw

from qwen_vlm_client import QwenVLMClient


def _fetch_bytes(url: str, timeout_sec: int) -> bytes:
    req = request.Request(url=url, method="GET")
    with request.urlopen(req, timeout=timeout_sec) as resp:
        return resp.read()


def _post_json(url: str, payload: Dict[str, Any], timeout_sec: int) -> Dict[str, Any]:
    req = request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _message_text(content: Any) -> str:
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text") is not None:
                parts.append(str(item["text"]))
        return "".join(parts).strip()
    return str(content).strip()


def _strip_code_fence(text: str) -> str:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()
    if candidate.lower().startswith("json"):
        candidate = candidate[4:].lstrip(" \n\r\t:")
    return candidate


def _candidate_fragments(text: str) -> List[str]:
    fragments: List[str] = []
    stripped = _strip_code_fence(text)
    if stripped:
        fragments.append(stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end >= start:
        fragment = stripped[start : end + 1].strip()
        if fragment and fragment not in fragments:
            fragments.append(fragment)
    return fragments


def _single_to_double_quoted_strings(text: str) -> str:
    pattern = re.compile(r"'([^'\\\\]*(?:\\\\.[^'\\\\]*)*)'")

    def _replace(match: re.Match) -> str:
        inner = match.group(1)
        inner = inner.replace('"', '\\"')
        return f'"{inner}"'

    return pattern.sub(_replace, text)


def _sanitize_json_like(text: str) -> str:
    candidate = _strip_code_fence(text)
    candidate = candidate.replace("\u201c", '"').replace("\u201d", '"')
    candidate = candidate.replace("\u2018", "'").replace("\u2019", "'")
    candidate = _single_to_double_quoted_strings(candidate)
    candidate = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)', r'\1"\2"\3', candidate)
    candidate = re.sub(r",(\s*[}\]])", r"\1", candidate)
    candidate = re.sub(r"\bTrue\b", "true", candidate)
    candidate = re.sub(r"\bFalse\b", "false", candidate)
    candidate = re.sub(r"\bNone\b", "null", candidate)
    return candidate.strip()


def _pythonize_literals(text: str) -> str:
    candidate = text
    candidate = re.sub(r"\btrue\b", "True", candidate)
    candidate = re.sub(r"\bfalse\b", "False", candidate)
    candidate = re.sub(r"\bnull\b", "None", candidate)
    return candidate


def _extract_json(text: Any) -> Dict[str, Any]:
    candidate = _message_text(text)
    errors: List[str] = []
    fragments = _candidate_fragments(candidate)
    if not fragments:
        raise RuntimeError(f"model did not return JSON: {candidate!r}")

    for fragment in fragments:
        for parser_name, parser_input in (
            ("json", fragment),
            ("json_sanitized", _sanitize_json_like(fragment)),
            ("python_literal", _pythonize_literals(fragment)),
            ("python_literal_sanitized", _pythonize_literals(_sanitize_json_like(fragment))),
        ):
            try:
                if parser_name.startswith("json"):
                    payload = json.loads(parser_input)
                else:
                    payload = ast.literal_eval(parser_input)
            except (json.JSONDecodeError, SyntaxError, ValueError) as exc:
                errors.append(f"{parser_name}: {exc}")
                continue
            if isinstance(payload, dict):
                return payload
            errors.append(f"{parser_name}: non-dict payload {type(payload).__name__}")

    error_summary = "; ".join(errors[-6:])
    raise RuntimeError(f"unable to parse model JSON. raw={candidate!r}; errors={error_summary}")


def _call_qwen_json(client: QwenVLMClient, image_bytes: bytes, model: str, prompt: str, timeout_sec: int) -> Dict[str, Any]:
    data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "temperature": 0,
        "enable_thinking": False,
    }
    req = request.Request(
        url=f"{client.base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {client.api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Qwen request failed with HTTP {exc.code}: {detail or exc.reason}") from exc
    message = payload["choices"][0]["message"]["content"]
    result = _extract_json(message)
    result["_model"] = payload.get("model", model)
    result["_usage"] = payload.get("usage", {})
    return result


def _point_from_any(value: Any) -> Tuple[float, float]:
    if isinstance(value, dict):
        return (float(value["x"]), float(value["y"]))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return (float(value[0]), float(value[1]))
    raise RuntimeError(f"invalid point value: {value!r}")


def _normalize_corners(payload: Dict[str, Any], width: int, height: int) -> Tuple[Dict[str, Dict[str, int]], str]:
    if not payload.get("found", True):
        raise RuntimeError(payload.get("reason") or "board not found")
    corners_raw = payload.get("corners")
    if not isinstance(corners_raw, dict):
        raise RuntimeError(f"model response missing corners: {payload}")

    ordered: Dict[str, Tuple[float, float]] = {}
    for key in ("tl", "tr", "br", "bl"):
        if key not in corners_raw:
            raise RuntimeError(f"corner {key} missing in model response")
        ordered[key] = _point_from_any(corners_raw[key])

    max_x = max(point[0] for point in ordered.values())
    max_y = max(point[1] for point in ordered.values())
    coordinate_space = "pixel"
    scale_x = 1.0
    scale_y = 1.0
    if max_x > (width - 1) or max_y > (height - 1):
        if max_x <= 1005.0 and max_y <= 1005.0:
            coordinate_space = "normalized_0_999"
            scale_x = float(width - 1) / 999.0
            scale_y = float(height - 1) / 999.0
        else:
            coordinate_space = "clamped"

    normalized: Dict[str, Dict[str, int]] = {}
    for key, (x_value, y_value) in ordered.items():
        if coordinate_space == "normalized_0_999":
            x_value *= scale_x
            y_value *= scale_y
        x_int = int(round(max(0.0, min(float(width - 1), x_value))))
        y_int = int(round(max(0.0, min(float(height - 1), y_value))))
        normalized[key] = {"x": x_int, "y": y_int}
    return normalized, coordinate_space


def _compute_crop_box(corners: Dict[str, Dict[str, int]], width: int, height: int, padding_px: int) -> Tuple[int, int, int, int]:
    xs = [point["x"] for point in corners.values()]
    ys = [point["y"] for point in corners.values()]
    left = max(0, min(xs) - padding_px)
    top = max(0, min(ys) - padding_px)
    right = min(width, max(xs) + padding_px)
    bottom = min(height, max(ys) + padding_px)
    if right <= left:
        right = min(width, left + 2)
    if bottom <= top:
        bottom = min(height, top + 2)
    return (left, top, right, bottom)


def _smooth_profile(values: np.ndarray) -> np.ndarray:
    kernel = np.array([1.0, 2.0, 3.0, 4.0, 3.0, 2.0, 1.0], dtype=np.float32)
    if values.size < kernel.size:
        return values.astype(np.float32)
    return np.convolve(values.astype(np.float32), kernel / kernel.sum(), mode="same")


def _refine_corner(gray: np.ndarray, seed: Tuple[int, int], horizontal_side: str, vertical_side: str) -> Tuple[int, int, Dict[str, Any]]:
    height, width = gray.shape[:2]
    half_window = max(18, min(32, min(height, width) // 4))
    x1 = max(0, int(seed[0]) - half_window)
    y1 = max(0, int(seed[1]) - half_window)
    x2 = min(width, int(seed[0]) + half_window + 1)
    y2 = min(height, int(seed[1]) + half_window + 1)
    roi = gray[y1:y2, x1:x2]
    threshold = int(max(35, min(100, np.percentile(roi, 20) + 10)))
    dark = roi <= threshold
    row_counts = _smooth_profile(dark.sum(axis=1))
    col_counts = _smooth_profile(dark.sum(axis=0))

    row_mid = max(1, row_counts.shape[0] // 2)
    col_mid = max(1, col_counts.shape[0] // 2)
    if horizontal_side == "top":
        row_index = int(np.argmax(row_counts[:row_mid]))
    else:
        row_index = int(np.argmax(row_counts[row_mid:]) + row_mid)
    if vertical_side == "left":
        col_index = int(np.argmax(col_counts[:col_mid]))
    else:
        col_index = int(np.argmax(col_counts[col_mid:]) + col_mid)

    local_y = row_index
    local_x = col_index
    y_low = max(0, row_index - 6)
    y_high = min(dark.shape[0], row_index + 7)
    x_low = max(0, col_index - 6)
    x_high = min(dark.shape[1], col_index + 7)
    patch = np.argwhere(dark[y_low:y_high, x_low:x_high])
    if patch.size > 0:
        patch = patch + np.array([y_low, x_low], dtype=np.int32)
        distances = np.sum((patch - np.array([row_index, col_index], dtype=np.int32)) ** 2, axis=1)
        best = patch[int(np.argmin(distances))]
        local_y = int(best[0])
        local_x = int(best[1])

    return (
        x1 + local_x,
        y1 + local_y,
        {
            "window": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
            "threshold": threshold,
            "row_peak": int(row_index),
            "col_peak": int(col_index),
        },
    )


def _refine_corners(crop_image: Image.Image, coarse_crop_corners: Dict[str, Dict[str, int]]) -> Tuple[Dict[str, Dict[str, int]], Dict[str, Any]]:
    gray = np.array(crop_image.convert("L"), dtype=np.uint8)
    refined: Dict[str, Dict[str, int]] = {}
    debug: Dict[str, Any] = {}
    specs = {
        "tl": ("top", "left"),
        "tr": ("top", "right"),
        "br": ("bottom", "right"),
        "bl": ("bottom", "left"),
    }
    for key, (horizontal_side, vertical_side) in specs.items():
        seed = (coarse_crop_corners[key]["x"], coarse_crop_corners[key]["y"])
        refined_x, refined_y, detail = _refine_corner(gray, seed, horizontal_side, vertical_side)
        refined[key] = {"x": refined_x, "y": refined_y}
        debug[key] = detail
    return refined, debug


def _refine_board_rectangle(crop_image: Image.Image, coarse_crop_corners: Dict[str, Dict[str, int]]) -> Tuple[Dict[str, Dict[str, int]], Dict[str, Any]]:
    gray = np.array(crop_image.convert("L"), dtype=np.float32)
    height, width = gray.shape[:2]
    left_seed = int(round((coarse_crop_corners["tl"]["x"] + coarse_crop_corners["bl"]["x"]) / 2.0))
    right_seed = int(round((coarse_crop_corners["tr"]["x"] + coarse_crop_corners["br"]["x"]) / 2.0))
    top_seed = int(round((coarse_crop_corners["tl"]["y"] + coarse_crop_corners["tr"]["y"]) / 2.0))
    bottom_seed = int(round((coarse_crop_corners["bl"]["y"] + coarse_crop_corners["br"]["y"]) / 2.0))

    inner_top = max(0, top_seed + 12)
    inner_bottom = min(height, bottom_seed - 12)
    if inner_bottom - inner_top < 20:
        inner_top = max(0, top_seed + 4)
        inner_bottom = min(height, bottom_seed - 4)
    if inner_bottom <= inner_top:
        inner_top = 0
        inner_bottom = height

    col_profile = gray[inner_top:inner_bottom, :].mean(axis=0)
    col_profile = _smooth_profile(col_profile)
    col_grad = np.diff(col_profile)

    left_start = max(0, left_seed - 50)
    left_end = min(width - 1, left_seed + 50)
    right_start = max(0, right_seed - 50)
    right_end = min(width - 1, right_seed + 50)
    if left_end <= left_start:
        left_start = 0
        left_end = max(1, width // 2)
    if right_end <= right_start:
        right_start = max(0, width // 2)
        right_end = width - 1

    left_edge = int(np.argmax(col_grad[left_start:left_end]) + left_start + 1)
    right_edge = int(np.argmin(col_grad[right_start:right_end]) + right_start + 1)

    inner_left = max(0, left_edge + 12)
    inner_right = min(width, right_edge - 12)
    if inner_right - inner_left < 20:
        inner_left = max(0, left_edge + 4)
        inner_right = min(width, right_edge - 4)
    if inner_right <= inner_left:
        inner_left = 0
        inner_right = width

    row_profile = gray[:, inner_left:inner_right].mean(axis=1)
    row_profile = _smooth_profile(row_profile)
    row_grad = np.diff(row_profile)

    top_start = max(0, top_seed - 50)
    top_end = min(height - 1, top_seed + 50)
    bottom_start = max(0, bottom_seed - 50)
    bottom_end = min(height - 1, bottom_seed + 50)
    if top_end <= top_start:
        top_start = 0
        top_end = max(1, height // 2)
    if bottom_end <= bottom_start:
        bottom_start = max(0, height // 2)
        bottom_end = height - 1

    top_edge = int(np.argmax(row_grad[top_start:top_end]) + top_start + 1)
    bottom_edge = int(np.argmin(row_grad[bottom_start:bottom_end]) + bottom_start + 1)

    corners = {
        "tl": {"x": left_edge, "y": top_edge},
        "tr": {"x": right_edge, "y": top_edge},
        "br": {"x": right_edge, "y": bottom_edge},
        "bl": {"x": left_edge, "y": bottom_edge},
    }
    debug = {
        "seed_edges": {
            "left": left_seed,
            "right": right_seed,
            "top": top_seed,
            "bottom": bottom_seed,
        },
        "detected_edges": {
            "left": left_edge,
            "right": right_edge,
            "top": top_edge,
            "bottom": bottom_edge,
        },
        "profiles": {
            "inner_top": inner_top,
            "inner_bottom": inner_bottom,
            "inner_left": inner_left,
            "inner_right": inner_right,
            "left_search": [left_start, left_end],
            "right_search": [right_start, right_end],
            "top_search": [top_start, top_end],
            "bottom_search": [bottom_start, bottom_end],
        },
    }
    return corners, debug


def _mean_point(points: List[Tuple[float, float]]) -> Dict[str, int]:
    x_value = int(round(sum(point[0] for point in points) / float(len(points))))
    y_value = int(round(sum(point[1] for point in points) / float(len(points))))
    return {"x": x_value, "y": y_value}


def _blend_rectangle_corners(
    coarse_full: Dict[str, Dict[str, int]],
    marker_full: Dict[str, Dict[str, int]],
) -> Tuple[Dict[str, Dict[str, int]], Dict[str, Any]]:
    left = int(round(np.median([coarse_full["tl"]["x"], coarse_full["bl"]["x"], marker_full["tl"]["x"], marker_full["bl"]["x"]])))
    right = int(round(np.median([coarse_full["tr"]["x"], coarse_full["br"]["x"], marker_full["tr"]["x"], marker_full["br"]["x"]])))
    top = int(round(np.median([coarse_full["tl"]["y"], coarse_full["tr"]["y"], marker_full["tl"]["y"], marker_full["tr"]["y"]])))
    bottom = int(round(np.median([coarse_full["bl"]["y"], coarse_full["br"]["y"], marker_full["bl"]["y"], marker_full["br"]["y"]])))
    corners = {
        "tl": {"x": left, "y": top},
        "tr": {"x": right, "y": top},
        "br": {"x": right, "y": bottom},
        "bl": {"x": left, "y": bottom},
    }
    debug = {
        "left_candidates": [coarse_full["tl"]["x"], coarse_full["bl"]["x"], marker_full["tl"]["x"], marker_full["bl"]["x"]],
        "right_candidates": [coarse_full["tr"]["x"], coarse_full["br"]["x"], marker_full["tr"]["x"], marker_full["br"]["x"]],
        "top_candidates": [coarse_full["tl"]["y"], coarse_full["tr"]["y"], marker_full["tl"]["y"], marker_full["tr"]["y"]],
        "bottom_candidates": [coarse_full["bl"]["y"], coarse_full["br"]["y"], marker_full["bl"]["y"], marker_full["br"]["y"]],
    }
    return corners, debug


def _bilinear_point(corners: Dict[str, Dict[str, int]], u: float, v: float) -> Dict[str, int]:
    tl = np.array([corners["tl"]["x"], corners["tl"]["y"]], dtype=np.float32)
    tr = np.array([corners["tr"]["x"], corners["tr"]["y"]], dtype=np.float32)
    br = np.array([corners["br"]["x"], corners["br"]["y"]], dtype=np.float32)
    bl = np.array([corners["bl"]["x"], corners["bl"]["y"]], dtype=np.float32)
    point = ((1.0 - u) * (1.0 - v) * tl) + (u * (1.0 - v) * tr) + (u * v * br) + ((1.0 - u) * v * bl)
    return {"x": int(round(float(point[0]))), "y": int(round(float(point[1])))}


def _query_depth(base_url: str, point: Dict[str, int], radius: int, timeout_sec: int) -> Dict[str, Any]:
    return _post_json(
        f"{base_url.rstrip('/')}/v1/query-depth",
        {"x": int(point["x"]), "y": int(point["y"]), "radius": int(radius)},
        timeout_sec,
    )


def _draw_cross(draw: ImageDraw.ImageDraw, x_value: int, y_value: int, color: Tuple[int, int, int], radius: int = 4) -> None:
    draw.line((x_value - radius, y_value, x_value + radius, y_value), fill=color, width=2)
    draw.line((x_value, y_value - radius, x_value, y_value + radius), fill=color, width=2)


def _save_debug_image(image: Image.Image, crop_box: Tuple[int, int, int, int], coarse: Dict[str, Dict[str, int]], refined: Dict[str, Dict[str, int]], center: Dict[str, int], path: str) -> None:
    debug_image = image.convert("RGB").copy()
    draw = ImageDraw.Draw(debug_image)
    draw.rectangle(crop_box, outline=(255, 215, 0), width=2)
    for point in coarse.values():
        _draw_cross(draw, point["x"], point["y"], (255, 64, 64), radius=5)
    for point in refined.values():
        _draw_cross(draw, point["x"], point["y"], (64, 255, 64), radius=5)
    _draw_cross(draw, center["x"], center["y"], (64, 224, 255), radius=6)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    debug_image.save(path, format="JPEG", quality=92)


def main() -> int:
    parser = argparse.ArgumentParser(description="Locate the white X-board with VLM and refine its four corners.")
    parser.add_argument("--base-url", default=os.environ.get("REALSENSE_BASE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--model", default=os.environ.get("QWEN_BOARD_MODEL", "qwen3-vl-plus"))
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("QWEN_VLM_TIMEOUT_SEC", "45") or "45"))
    parser.add_argument("--crop-padding", type=int, default=36)
    parser.add_argument("--depth-radius", type=int, default=2)
    parser.add_argument("--debug-image", default="")
    parser.add_argument("--output", default="", help="Optional JSON output path")
    args = parser.parse_args()

    client = QwenVLMClient.from_env()
    if not client.configured:
        raise RuntimeError("DASHSCOPE_API_KEY is not configured")

    snapshot_url = f"{args.base_url.rstrip('/')}/v1/realsense-snapshot"
    image_bytes = _fetch_bytes(snapshot_url, args.timeout_sec)
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = image.size

    prompt = (
        f"图像尺寸为 {width}x{height}。请定位图中带黑色X的白色板子的四个外轮廓角点。"
        "只输出JSON，不要输出Markdown，不要解释。返回格式："
        '{"found": true, "corners": {"tl": {"x": 0, "y": 0}, "tr": {"x": 0, "y": 0}, "br": {"x": 0, "y": 0}, "bl": {"x": 0, "y": 0}}, "confidence": 0.0, "reason": ""}。'
        "所有键名和字符串都必须使用双引号。"
        "如果你返回的是归一化到0到999的坐标也可以，但必须保持左上、右上、右下、左下顺序。"
    )
    vlm_payload = _call_qwen_json(client, image_bytes, args.model, prompt, args.timeout_sec)
    coarse_corners, coordinate_space = _normalize_corners(vlm_payload, width, height)

    crop_box = _compute_crop_box(coarse_corners, width, height, args.crop_padding)
    crop_image = image.crop(crop_box)
    coarse_crop = {
        key: {"x": point["x"] - crop_box[0], "y": point["y"] - crop_box[1]}
        for key, point in coarse_corners.items()
    }
    marker_refined_crop, marker_refinement_debug = _refine_corners(crop_image, coarse_crop)
    edge_refined_crop, edge_refinement_debug = _refine_board_rectangle(crop_image, coarse_crop)
    edge_refined_full = {
        key: {"x": point["x"] + crop_box[0], "y": point["y"] + crop_box[1]}
        for key, point in edge_refined_crop.items()
    }
    marker_refined_full = {
        key: {"x": point["x"] + crop_box[0], "y": point["y"] + crop_box[1]}
        for key, point in marker_refined_crop.items()
    }
    refined_full, blended_refinement_debug = _blend_rectangle_corners(coarse_corners, marker_refined_full)

    center = _mean_point([(point["x"], point["y"]) for point in refined_full.values()])
    plane_sample_specs = [
        ("center", center),
        ("upper_mid", _bilinear_point(refined_full, 0.5, 0.25)),
        ("lower_mid", _bilinear_point(refined_full, 0.5, 0.75)),
        ("left_mid", _bilinear_point(refined_full, 0.25, 0.5)),
        ("right_mid", _bilinear_point(refined_full, 0.75, 0.5)),
    ]

    depth = {
        "center": _query_depth(args.base_url, center, args.depth_radius, args.timeout_sec),
        "corners": {
            key: _query_depth(args.base_url, point, args.depth_radius, args.timeout_sec)
            for key, point in refined_full.items()
        },
        "plane_samples": [
            {
                "name": name,
                "pixel": point,
                "query": _query_depth(args.base_url, point, args.depth_radius, args.timeout_sec),
            }
            for name, point in plane_sample_specs
        ],
    }

    crop_width = max(refined_full["tr"]["x"] - refined_full["tl"]["x"], refined_full["br"]["x"] - refined_full["bl"]["x"])
    crop_height = max(refined_full["bl"]["y"] - refined_full["tl"]["y"], refined_full["br"]["y"] - refined_full["tr"]["y"])

    if args.debug_image:
        _save_debug_image(image, crop_box, coarse_corners, refined_full, center, args.debug_image)

    result = {
        "timestamp_ms": int(time.time() * 1000),
        "image": {"width": width, "height": height, "snapshot_url": snapshot_url},
        "model": args.model,
        "vlm": {
            "coordinate_space": coordinate_space,
            "payload": {key: value for key, value in vlm_payload.items() if not key.startswith("_")},
            "usage": vlm_payload.get("_usage", {}),
        },
        "board": {
            "coarse_corners_px": coarse_corners,
            "refined_corners_px": refined_full,
            "center_px": center,
            "crop_box_px": {"x1": crop_box[0], "y1": crop_box[1], "x2": crop_box[2], "y2": crop_box[3]},
            "approx_size_px": {"width": int(crop_width), "height": int(crop_height)},
            "edge_refinement": edge_refinement_debug,
            "edge_refined_corners_px": edge_refined_full,
            "marker_refinement": marker_refinement_debug,
            "marker_refined_corners_px": marker_refined_full,
            "blended_refinement": blended_refinement_debug,
        },
        "depth": depth,
    }

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
