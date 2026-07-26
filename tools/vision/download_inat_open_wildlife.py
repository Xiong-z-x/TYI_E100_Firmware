#!/usr/bin/env python3
"""Download license-filtered iNaturalist wildlife images for NUEDC training."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import requests
from PIL import Image


API_URL = "https://api.inaturalist.org/v1/observations"
ALLOWED_LICENSES = {"cc0", "cc-by"}
CLASS_TAXA: Dict[str, Sequence[Tuple[int, int]]] = {
    "elephant": ((43692, 260),),
    "tiger": ((41967, 220),),
    "wolf": ((42048, 260),),
    "monkey": ((43443, 130), (554251, 130)),
    "peacock": ((1199, 260),),
}


def _request_json(session: requests.Session, params: Dict[str, Any]) -> Dict[str, Any]:
    for attempt in range(5):
        try:
            response = session.get(API_URL, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt == 4:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def _candidate_pages(total: int, needed: int, seed: int) -> List[int]:
    per_page = 200
    # The public API refuses deep pagination beyond 10,000 observations.
    page_count = min(50, max(1, math.ceil(total / per_page)))
    wanted_pages = min(page_count, max(3, math.ceil(needed * 2.5 / per_page)))
    if wanted_pages >= page_count:
        return list(range(1, page_count + 1))
    rng = random.Random(seed)
    anchors = {1, page_count, max(1, page_count // 2)}
    population = [page for page in range(1, page_count + 1) if page not in anchors]
    anchors.update(rng.sample(population, min(len(population), wanted_pages - len(anchors))))
    return sorted(anchors)


def _fetch_taxon_candidates(
    session: requests.Session, taxon_id: int, needed: int, seed: int
) -> List[Dict[str, Any]]:
    base_params: Dict[str, Any] = {
        "taxon_id": taxon_id,
        "quality_grade": "research",
        "photos": "true",
        "photo_license": "cc0,cc-by",
        "order_by": "created_at",
        "order": "desc",
    }
    first = _request_json(session, {**base_params, "per_page": 1, "page": 1})
    total = int(first.get("total_results", 0))
    candidates: Dict[int, Dict[str, Any]] = {}
    for page in _candidate_pages(total, needed, seed + taxon_id):
        payload = _request_json(
            session, {**base_params, "per_page": 200, "page": page}
        )
        for observation in payload.get("results", []):
            photos = observation.get("photos") or []
            if not photos:
                continue
            photo = max(
                photos,
                key=lambda item: int(
                    (item.get("original_dimensions") or {}).get("width", 0)
                )
                * int((item.get("original_dimensions") or {}).get("height", 0)),
            )
            license_code = str(photo.get("license_code") or "").lower()
            url = str(photo.get("url") or "")
            dimensions = photo.get("original_dimensions") or {}
            width = int(dimensions.get("width", 0))
            height = int(dimensions.get("height", 0))
            if license_code not in ALLOWED_LICENSES:
                continue
            if "inaturalist-open-data" not in url:
                continue
            if min(width, height) < 512 or max(width, height) / max(1, min(width, height)) > 3.2:
                continue
            photo_id = int(photo["id"])
            candidates[photo_id] = {
                "photoId": photo_id,
                "observationId": int(observation["id"]),
                "taxonId": taxon_id,
                "license": license_code,
                "attribution": photo.get("attribution") or "",
                "sourceUrl": url.replace("square.", "large."),
                "width": width,
                "height": height,
            }
        time.sleep(0.8)

    ordered = list(candidates.values())
    random.Random(seed + taxon_id * 17).shuffle(ordered)
    return ordered[:needed]


def _assign_splits(items: Sequence[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    ordered = sorted(items, key=lambda item: int(item["photoId"]))
    for index, item in enumerate(ordered):
        fraction = index / max(1, len(ordered))
        split = "train" if fraction < 0.80 else "val" if fraction < 0.90 else "test"
        yield {**item, "split": split}


def _download_one(
    session: requests.Session, root: Path, class_name: str, item: Dict[str, Any]
) -> Dict[str, Any]:
    destination = (
        root
        / "images"
        / str(item["split"])
        / class_name
        / f"inat-{int(item['photoId'])}.jpg"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_file():
        for attempt in range(5):
            try:
                response = session.get(str(item["sourceUrl"]), timeout=45)
                response.raise_for_status()
                destination.write_bytes(response.content)
                with Image.open(destination) as image:
                    image = image.convert("RGB")
                    image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                    image.save(destination, format="JPEG", quality=92, optimize=True)
                break
            except (requests.RequestException, OSError):
                destination.unlink(missing_ok=True)
                if attempt == 4:
                    raise
                time.sleep(2**attempt)
    relative = destination.relative_to(root).as_posix()
    return {**item, "class": class_name, "file": relative}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=26072651)
    args = parser.parse_args()

    output_root = Path(args.output).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "TYI-E100-NUEDC-research/1.0 (dataset preparation)"}
    )

    selected: List[Tuple[str, Dict[str, Any]]] = []
    for class_index, (class_name, taxa) in enumerate(CLASS_TAXA.items()):
        class_items: List[Dict[str, Any]] = []
        for taxon_id, count in taxa:
            class_items.extend(
                _fetch_taxon_candidates(
                    session, taxon_id, count, args.seed + class_index * 1000
                )
            )
        for item in _assign_splits(class_items):
            selected.append((class_name, item))

    downloaded: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(_download_one, session, output_root, class_name, item)
            for class_name, item in selected
        ]
        for future in as_completed(futures):
            downloaded.append(future.result())

    downloaded.sort(key=lambda item: (item["class"], item["split"], item["file"]))
    manifest_path = output_root / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as handle:
        for item in downloaded:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    counts: Dict[str, int] = {}
    for item in downloaded:
        key = f"{item['split']}:{item['class']}"
        counts[key] = counts.get(key, 0) + 1
    summary = {
        "source": "iNaturalist Open Data API",
        "licenses": sorted(ALLOWED_LICENSES),
        "seed": args.seed,
        "counts": counts,
        "total": len(downloaded),
        "manifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
