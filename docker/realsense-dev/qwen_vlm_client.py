#!/usr/bin/env python3
import ast
import base64
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, request


def bool_env(name: str, fallback: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return fallback
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text") is not None:
                parts.append(str(item["text"]))
        return "".join(parts)
    raise RuntimeError("Unexpected model response format")


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


def _normalize_coordinate_space_name(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"normalized_0_999", "normalized", "0_999", "0-999", "grid_0_999", "grid"}:
        return "normalized_0_999"
    if normalized in {"pixel", "pixels", "absolute_pixel", "absolute_pixels"}:
        return "pixel"
    return ""


def _scale_coordinate_pair(
    x_value: float,
    y_value: float,
    width: int,
    height: int,
    coordinate_space_hint: str = "",
) -> Tuple[float, float, str]:
    coordinate_space = _normalize_coordinate_space_name(coordinate_space_hint) or "pixel"
    if coordinate_space == "normalized_0_999":
        x_value *= float(width - 1) / 999.0
        y_value *= float(height - 1) / 999.0
        return x_value, y_value, "normalized_0_999"
    if (x_value > (width - 1) or y_value > (height - 1)) and x_value <= 1005.0 and y_value <= 1005.0:
        x_value *= float(width - 1) / 999.0
        y_value *= float(height - 1) / 999.0
        return x_value, y_value, "normalized_0_999"
    return x_value, y_value, "pixel"


class QwenVLMClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_sec: int = 45,
        enable_thinking: bool = False,
        disable_proxy: bool = True,
        coordinate_mode: str = "normalized_0_999",
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip() or "qwen3.6-plus"
        self.timeout_sec = max(5, int(timeout_sec))
        self.enable_thinking = enable_thinking
        self.disable_proxy = disable_proxy
        self.coordinate_mode = _normalize_coordinate_space_name(coordinate_mode) or "normalized_0_999"

    @classmethod
    def from_env(cls) -> "QwenVLMClient":
        return cls(
            api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
            base_url=os.environ.get("QWEN_VLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            model=os.environ.get("QWEN_VLM_MODEL", "qwen3.6-plus"),
            timeout_sec=int(os.environ.get("QWEN_VLM_TIMEOUT_SEC", "45") or "45"),
            enable_thinking=bool_env("QWEN_VLM_ENABLE_THINKING", False),
            disable_proxy=bool_env("QWEN_VLM_DISABLE_PROXY", True),
            coordinate_mode=os.environ.get("QWEN_VLM_COORDINATE_MODE", "normalized_0_999"),
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _extract_text(self, content: Any) -> str:
        return _message_text(content)

    def _extract_json(self, text: str) -> Dict[str, Any]:
        candidate = _message_text(text)
        errors: List[str] = []
        fragments = _candidate_fragments(candidate)
        if not fragments:
            raise RuntimeError(f"Model did not return JSON: {candidate!r}")

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
        raise RuntimeError(f"Unable to parse model JSON. raw={candidate!r}; errors={error_summary}")

    def _clamp_int(self, value: Any, lower: int, upper: int) -> int:
        return max(lower, min(int(round(float(value))), upper))

    def _normalize_bbox(
        self,
        bbox: Any,
        width: int,
        height: int,
        coordinate_space_hint: str,
    ) -> Tuple[Optional[Dict[str, int]], str]:
        if bbox is None:
            return None, "unknown"
        if isinstance(bbox, dict):
            x1 = bbox.get("x1", bbox.get("left", bbox.get("xmin")))
            y1 = bbox.get("y1", bbox.get("top", bbox.get("ymin")))
            x2 = bbox.get("x2", bbox.get("right", bbox.get("xmax")))
            y2 = bbox.get("y2", bbox.get("bottom", bbox.get("ymax")))
        elif isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            x1, y1, x2, y2 = bbox[:4]
        else:
            return None, "unknown"
        if None in {x1, y1, x2, y2}:
            return None, "unknown"
        x1f, y1f, coord_a = _scale_coordinate_pair(float(x1), float(y1), width, height, coordinate_space_hint)
        x2f, y2f, coord_b = _scale_coordinate_pair(float(x2), float(y2), width, height, coordinate_space_hint)
        coordinate_space = "normalized_0_999" if "normalized_0_999" in {coord_a, coord_b} else "pixel"
        left = self._clamp_int(x1f, 0, width - 1)
        top = self._clamp_int(y1f, 0, height - 1)
        right = self._clamp_int(x2f, 0, width - 1)
        bottom = self._clamp_int(y2f, 0, height - 1)
        if right <= left:
            right = min(width - 1, left + 1)
        if bottom <= top:
            bottom = min(height - 1, top + 1)
        return {"x1": left, "y1": top, "x2": right, "y2": bottom}, coordinate_space

    def _normalize_point(
        self,
        point: Any,
        width: int,
        height: int,
        bbox: Optional[Dict[str, int]],
        coordinate_space_hint: str,
    ) -> Tuple[Optional[Dict[str, int]], str]:
        if isinstance(point, dict) and point.get("x") is not None and point.get("y") is not None:
            x_value, y_value, coordinate_space = _scale_coordinate_pair(float(point["x"]), float(point["y"]), width, height, coordinate_space_hint)
            return {
                "x": self._clamp_int(x_value, 0, width - 1),
                "y": self._clamp_int(y_value, 0, height - 1),
            }, coordinate_space
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            x_value, y_value, coordinate_space = _scale_coordinate_pair(float(point[0]), float(point[1]), width, height, coordinate_space_hint)
            return {
                "x": self._clamp_int(x_value, 0, width - 1),
                "y": self._clamp_int(y_value, 0, height - 1),
            }, coordinate_space
        if bbox is None:
            return None, "unknown"
        return {
            "x": (bbox["x1"] + bbox["x2"]) // 2,
            "y": (bbox["y1"] + bbox["y2"]) // 2,
        }, "derived_from_bbox"

    def _normalize_result(self, payload: Dict[str, Any], width: int, height: int) -> Dict[str, Any]:
        found_raw = payload.get("found")
        if isinstance(found_raw, bool):
            found = found_raw
        elif isinstance(found_raw, str):
            normalized_found = found_raw.strip().lower()
            if normalized_found in {"true", "1", "yes"}:
                found = True
            elif normalized_found in {"false", "0", "no"}:
                found = False
            else:
                found = False
        elif isinstance(found_raw, (int, float)):
            found = bool(found_raw)
        else:
            found = False
        reason = str(payload.get("reason", "")).strip()
        if not found:
            return {"found": False, "reason": reason or "target not found"}

        coordinate_space_hint = (
            _normalize_coordinate_space_name(
                payload.get("coordinateSpace")
                or payload.get("coordinate_space")
                or payload.get("coordinateMode")
                or payload.get("coordinate_mode")
            )
            or self.coordinate_mode
        )
        bbox, bbox_coordinate_space = self._normalize_bbox(payload.get("bbox"), width, height, coordinate_space_hint)
        point, point_coordinate_space = self._normalize_point(payload.get("point"), width, height, bbox, coordinate_space_hint)
        if point is None:
            raise RuntimeError("Model response did not include a usable point or bbox")

        confidence_raw = payload.get("confidence", 0.0)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        result: Dict[str, Any] = {
            "found": True,
            "label": str(payload.get("label", "")).strip(),
            "point": point,
            "confidence": round(confidence, 4),
            "reason": reason,
            "coordinateSpace": "pixel",
            "sourceCoordinateSpace": point_coordinate_space if point_coordinate_space != "unknown" else bbox_coordinate_space,
        }
        if bbox is not None:
            result["bbox"] = bbox
        return result

    def _build_prompt(self, instruction: str, width: int, height: int) -> str:
        return (
            "你是一个单目标视觉 grounding 模块。请在图像中只选择一个与用户描述最匹配的目标实例，只输出 JSON，不要输出 Markdown，不要解释。\n"
            f"图像尺寸：宽 {width} 像素，高 {height} 像素。\n"
            f"用户目标：{instruction.strip()}\n"
            "返回规则：\n"
            "1. 坐标必须是相对于原图左上角的绝对像素坐标。\n"
            '2. 如果找到目标，只返回一个 JSON 对象：{"found": true, "label": "目标名称", "bbox": [x1, y1, x2, y2], "point": [x, y], "confidence": 0.0, "reason": "简短说明"}。\n'
            "3. 如果是复合描述，必须同时满足主体、属性、颜色、材质、容器关系等关键约束；只满足一部分时必须返回 found=false。\n"
            "4. bbox 必须尽量贴合目标主体，不要包含无关背景，不要把墙面、地面、天花板、纯色背景、桌面边缘当作候选替代目标。\n"
            "5. point 必须落在目标可见区域内部，优先选择纹理清晰、边缘稳定、适合深度采样的位置。\n"
            "6. 对于透明、半透明、网状或反光目标，point 必须落在最不透明、最能代表实体内容的位置，不要落在可穿透看到背景的位置。\n"
            '7. 如果无法明确确认目标存在，或目标太小、太模糊、被严重遮挡、只能猜测，返回 {"found": false, "reason": "原因"}。\n'
            "8. 必须始终显式输出 found 字段。\n"
            "9. 不要输出任何 JSON 之外的文字。"
        )

    def ground(self, image_bytes: bytes, width: int, height: int, instruction: str, model: Optional[str] = None) -> Dict[str, Any]:
        if not self.configured:
            raise RuntimeError("DASHSCOPE_API_KEY is not configured")
        active_model = (model or self.model).strip() or self.model
        data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
        body: Dict[str, Any] = {
            "model": active_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": self._build_prompt(instruction, width, height)},
                    ],
                }
            ],
            "temperature": 0,
            "enable_thinking": self.enable_thinking,
        }
        request_body = json.dumps(body).encode("utf-8")
        req = request.Request(
            url=f"{self.base_url}/chat/completions",
            data=request_body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        opener = request.build_opener(request.ProxyHandler({})) if self.disable_proxy else request.build_opener()
        try:
            with opener.open(req, timeout=self.timeout_sec) as resp:
                response_payload = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Qwen request failed with HTTP {exc.code}: {detail or exc.reason}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Qwen request failed: {exc.reason}") from exc

        try:
            message = response_payload["choices"][0]["message"]
            text = self._extract_text(message["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected Qwen response: {response_payload}") from exc

        parsed = self._extract_json(text)
        result = self._normalize_result(parsed, width, height)
        result["model"] = response_payload.get("model", active_model)
        result["usage"] = response_payload.get("usage", {})
        return result
