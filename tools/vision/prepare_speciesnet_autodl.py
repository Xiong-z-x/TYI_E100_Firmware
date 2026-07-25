#!/usr/bin/env python3
"""Prepare a generated crop dataset for the official SpeciesNet fine-tuner."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Dict, Iterable, List, Sequence


DETECTION_CATEGORIES = {
    "1": "animal",
    "2": "person",
    "3": "vehicle",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            for key in ("split", "label", "relativePath"):
                if key not in record:
                    raise ValueError(
                        f"{path}:{line_number} is missing required field {key}"
                    )
            records.append(record)
    if not records:
        raise ValueError(f"manifest is empty: {path}")
    return records


def write_csv(path: Path, records: Iterable[Dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("filename", "category", "location"),
        )
        writer.writeheader()
        for record in records:
            split = str(record["split"])
            writer.writerow(
                {
                    "filename": str(record["relativePath"]),
                    "category": str(record["label"]),
                    # Two explicit locations make the official greedy splitter
                    # choose the held-out validation set as one indivisible unit.
                    "location": (
                        "synthetic-train-poses-0-1"
                        if split == "train"
                        else "synthetic-val-heldout-pose-2"
                    ),
                }
            )
            count += 1
    return count


def write_md_results(
    path: Path,
    records: Sequence[Dict[str, Any]],
    model_name: str,
) -> int:
    images = []
    for record in records:
        images.append(
            {
                "file": str(record["relativePath"]),
                "max_detection_conf": 1.0,
                "detections": [
                    {
                        "category": "1",
                        "conf": 1.0,
                        # Dataset images already simulate the tight crop that
                        # MegaDetector supplies to the SpeciesNet classifier.
                        "bbox": [0.0, 0.0, 1.0, 1.0],
                    }
                ],
            }
        )
    payload = {
        "info": {
            "format_version": "1.4",
            "detector": model_name,
            "note": (
                "Synthetic pre-crops: one full-image animal box per sample. "
                "Unknown samples emulate detector false-positive crops."
            ),
        },
        "detection_categories": DETECTION_CATEGORIES,
        "images": images,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
    return len(images)


def write_run_scripts(output: Path) -> None:
    common = """#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FINETUNER="${FINETUNER:-$ROOT/../speciesnet-fine-tuning}"
RUN_ROOT="${RUN_ROOT:-$ROOT/runs}"
BACKBONE="${BACKBONE:-$ROOT/autodl/speciesnet_timm.pt}"
mkdir -p "$RUN_ROOT"
"""
    stage1 = common + """

python "$FINETUNER/scripts/train.py" \
  --data-csv "$ROOT/autodl/train-val.csv" \
  --image-root "$ROOT" \
  --md-results "$ROOT/autodl/train-val-md.json" \
  --run-folder "$RUN_ROOT/speciesnet-closed-set-stage1" \
  --backbone-checkpoint "$BACKBONE" \
  --val-fraction 0.1666666667 \
  --min-instances 100 \
  --conf-threshold 0.3 \
  --max-boxes 1 \
  --epochs 12 \
  --unfreeze-blocks 0 \
  --batch-size "${BATCH_SIZE:-16}" \
  --workers "${WORKERS:-8}" \
  --lr 1e-4 \
  --patience 4
"""
    stage2 = common + """

STAGE1="$RUN_ROOT/speciesnet-closed-set-stage1/model_best.pt"
if [[ ! -f "$STAGE1" ]]; then
  echo "Stage 1 checkpoint not found: $STAGE1" >&2
  exit 2
fi

python "$FINETUNER/scripts/train.py" \
  --data-csv "$ROOT/autodl/train-val.csv" \
  --image-root "$ROOT" \
  --md-results "$ROOT/autodl/train-val-md.json" \
  --run-folder "$RUN_ROOT/speciesnet-closed-set-stage2" \
  --backbone-checkpoint "$STAGE1" \
  --val-fraction 0.1666666667 \
  --min-instances 100 \
  --conf-threshold 0.3 \
  --max-boxes 1 \
  --epochs 24 \
  --unfreeze-blocks 2 \
  --batch-size "${BATCH_SIZE:-16}" \
  --workers "${WORKERS:-8}" \
  --lr 5e-5 \
  --patience 5
"""
    (output / "run_stage1.sh").write_text(
        stage1,
        encoding="utf-8",
        newline="\n",
    )
    (output / "run_stage2.sh").write_text(
        stage2,
        encoding="utf-8",
        newline="\n",
    )


def write_readme(path: Path, counts: Dict[str, int]) -> None:
    content = f"""# AutoDL SpeciesNet 闭集微调包

本目录由 `prepare_speciesnet_autodl.py` 生成，数据源是 MegaDetector 紧裁剪空间，
不是整幅场景空间。

- 训练图像：{counts.get("train", 0)}
- 验证图像：{counts.get("val", 0)}
- 独立合成测试图像：{counts.get("test", 0)}
- 类别：elephant、tiger、wolf、monkey、peacock、unknown

`train-val.csv` 只包含 train/val；`test.csv` 永不参与训练。
unknown 是背景硬负样本代理，最终仍要用 Nano 实拍误检框替换或补充。

建议先运行 `run_stage1.sh` 只训练新分类头。Stage 1 验证稳定后，再从其
`model_best.pt` 继续做 `unfreeze-blocks=2` 的 Stage 2；不要直接在合成集上
全量解冻。正式成绩必须以现场独立拍摄数据为准。
"""
    path.write_text(content, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output")
    parser.add_argument(
        "--backbone",
        help="Optional local speciesnet_timm.pt to copy into the package.",
    )
    args = parser.parse_args()

    dataset = Path(args.dataset).resolve()
    manifest = dataset / "manifest.jsonl"
    if not manifest.is_file():
        raise FileNotFoundError(manifest)
    output = (
        Path(args.output).resolve()
        if args.output
        else dataset / "autodl"
    )
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output: {output}")
    output.mkdir(parents=True, exist_ok=True)

    records = read_manifest(manifest)
    train_val = [
        record for record in records if record["split"] in ("train", "val")
    ]
    test = [record for record in records if record["split"] == "test"]
    counts: Dict[str, int] = {}
    for record in records:
        split = str(record["split"])
        counts[split] = counts.get(split, 0) + 1

    write_csv(output / "train-val.csv", train_val)
    write_csv(output / "test.csv", test)
    write_md_results(
        output / "train-val-md.json",
        train_val,
        "synthetic-full-box-for-pre-cropped-speciesnet-v2",
    )
    write_md_results(
        output / "test-md.json",
        test,
        "synthetic-full-box-for-pre-cropped-speciesnet-v2",
    )
    write_run_scripts(output)
    write_readme(output / "README.md", counts)
    if args.backbone:
        backbone = Path(args.backbone).resolve()
        if not backbone.is_file():
            raise FileNotFoundError(backbone)
        shutil.copy2(backbone, output / "speciesnet_timm.pt")

    generated = sorted(
        path for path in output.iterdir() if path.is_file()
    )
    manifest_payload = {
        "dataset": str(dataset),
        "sourceManifestSha256": sha256(manifest),
        "counts": counts,
        "files": {
            path.name: {
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in generated
        },
        "fineTuner": {
            "repository": (
                "https://github.com/agentmorris/speciesnet-fine-tuning"
            ),
            "verifiedCommit": "70e96884ce9981caca1ddc40bcebe59b0d011946",
        },
    }
    with (output / "package-manifest.json").open(
        "w",
        encoding="utf-8",
    ) as handle:
        json.dump(manifest_payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(manifest_payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
