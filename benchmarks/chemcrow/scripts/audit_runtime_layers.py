"""Bounded all-layer secret/notice inventory; does not authorize public redistribution."""

import argparse
import hashlib
import json
import re
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--archive", required=True, type=Path)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
archive = args.archive
from openevo.runtime.managed import verify_managed_runtime_archive

verify_managed_runtime_archive(archive)

report = {
    "checked_at": datetime.now(UTC).isoformat(),
    "archive_sha256": hashlib.file_digest(archive.open("rb"), "sha256").hexdigest(),
    "layers": 0,
    "members": 0,
    "sensitive_paths": [],
    "secret_pattern_findings": [],
    "license_files": [],
    "scope": "all archive layers, including files removed by later layers; selected text files <=2 MiB; no credentials mounted",
}
secret = re.compile(
    rb"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-(?:or-v1-)?[A-Za-z0-9_-]{32,}|\bgh[pousr]_[A-Za-z0-9]{32,})"
)
with tarfile.open(archive, "r|gz") as outer:
    for member in outer:
        if not member.isfile() or not member.name.startswith("blobs/"):
            continue
        with tempfile.TemporaryFile() as f:
            shutil.copyfileobj(outer.extractfile(member), f)
            f.seek(0)
            start = f.read(1)
            f.seek(0)
            if start in (b"{", b"["):
                data = f.read()
                if secret.search(data):
                    report["secret_pattern_findings"].append(
                        {"path": member.name, "context": "image_metadata"}
                    )
                continue
            try:
                with tarfile.open(fileobj=f, mode="r|*") as layer:
                    report["layers"] += 1
                    for item in layer:
                        report["members"] += 1
                        name = item.name
                        if (
                            re.search(
                                r"(?:^|/)(?:auth\.json|credentials|id_rsa|id_ed25519|\.bash_history|\.zsh_history)$",
                                name,
                            )
                            or "/.aws/" in name
                            or "/.codex/" in name
                        ):
                            report["sensitive_paths"].append({"path": name, "size": item.size})
                        if item.isfile() and 0 < item.size <= 2 * 1024 * 1024:
                            license_file = bool(
                                re.search(
                                    r"(?:LICENSE|COPYING|NOTICE|copyright)(?:\.|$)",
                                    name,
                                    re.IGNORECASE,
                                )
                            )
                            inspect = license_file or name.endswith(
                                (
                                    ".json",
                                    ".toml",
                                    ".yaml",
                                    ".yml",
                                    ".env",
                                    ".pem",
                                    ".cfg",
                                    ".ini",
                                    ".sh",
                                )
                            )
                            if inspect:
                                data = layer.extractfile(item).read()
                                if secret.search(data):
                                    report["secret_pattern_findings"].append(
                                        {"path": name, "context": "text_file"}
                                    )
                                if license_file:
                                    report["license_files"].append(
                                        {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
                                    )
            except tarfile.ReadError:
                pass
report["automated_secret_scan"] = (
    "PASS"
    if not (report["sensitive_paths"] or report["secret_pattern_findings"])
    else "REVIEW_REQUIRED"
)
report["redistribution_clearance"] = "PENDING_LICENSE_OBLIGATION_REVIEW"
p = args.output
p.write_text(json.dumps(report, indent=2) + "\n")
p.chmod(0o600)
print(json.dumps({k: v for k, v in report.items() if k != "license_files"}))
print("license_files", len(report["license_files"]))
