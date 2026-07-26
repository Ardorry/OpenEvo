#!/usr/bin/env python3
"""Download and verify only the frozen AI4Chem/ChemBench4K v2 revision."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from openevo_chembench.chembench4k_dataset import (  # noqa: E402
    DATASET_MANIFEST_FILENAME,
    build_dataset_manifest,
    write_dataset_manifest,
)
from openevo_chembench.chembench4k_models import (  # noqa: E402
    CHEMBENCH4K_REPOSITORY,
    CHEMBENCH4K_REVISION,
)


DEFAULT_SNAPSHOT_ROOT = (
    WORKSPACE_ROOT / "data" / "chembench4k" / "AI4Chem_ChemBench4K" / CHEMBENCH4K_REVISION
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch and verify the exact frozen ChemBench4K v2 snapshot."
    )
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=DEFAULT_SNAPSHOT_ROOT,
        help="revision-named destination directory",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="never contact Hugging Face; verify already downloaded files",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    snapshot_root = args.snapshot_root.resolve()
    if snapshot_root.name != CHEMBENCH4K_REVISION:
        raise SystemExit("snapshot root must end with the frozen dataset revision")

    if not args.verify_only:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=CHEMBENCH4K_REPOSITORY,
            repo_type="dataset",
            revision=CHEMBENCH4K_REVISION,
            allow_patterns=["dev/*.json", "test/*.json"],
            local_dir=snapshot_root,
        )

    output = write_dataset_manifest(snapshot_root)
    manifest = build_dataset_manifest(snapshot_root)
    receipt = {
        "status": "PASS",
        "repository": manifest.repository,
        "revision": manifest.revision,
        "dev_count": manifest.dev_count,
        "test_count": manifest.test_count,
        "combined_sha256": manifest.combined_sha256,
        "manifest_path": str(output),
        "manifest_filename": DATASET_MANIFEST_FILENAME,
        "model_calls_made": 0,
    }
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
