"""Acquire the original runtime without confusing local and public verification."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from openevo.runtime.managed import (
    MANAGED_RUNTIME_ARCHIVE_RELEASE as RELEASE,
)
from openevo.runtime.managed import (
    verify_managed_runtime_archive,
)


def install_runtime(
    *, archive: Path | None, url: str | None, cache: Path, load: bool = True
) -> dict:
    if (archive is None) == (url is None):
        raise ValueError("supply exactly one of --archive or --url")
    source = "local_archive"
    if url is not None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query:
            raise ValueError("runtime URL must be public HTTPS without credentials or query")
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive = cache / RELEASE.filename
        if not archive.exists():
            with tempfile.TemporaryDirectory(prefix=".runtime-", dir=cache) as staging:
                temporary = Path(staging) / RELEASE.filename
                with temporary.open("xb") as output:
                    temporary.chmod(0o600)
                    # No provider credentials or GitHub tokens are attached.
                    with (
                        httpx.Client(follow_redirects=True, timeout=120) as client,
                        client.stream("GET", url) as response,
                    ):
                        response.raise_for_status()
                        received = 0
                        for chunk in response.iter_bytes(1024 * 1024):
                            received += len(chunk)
                            if received > RELEASE.byte_size:
                                raise ValueError("runtime exceeds locked archive size")
                            output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                verify_managed_runtime_archive(temporary)
                # Exclusive publication: never replace an existing cache entry.
                os.link(temporary, archive)
                temporary.unlink()
                source = "anonymous_https_download"
        else:
            source = "verified_cache_not_new_download"
    assert archive is not None
    authority = verify_managed_runtime_archive(archive)
    report = {
        "schema_version": "chemcrowrun_runtime_install_v1",
        "status": "ARCHIVE_VERIFIED",
        "source": source,
        "anonymous_download_verified_this_invocation": source == "anonymous_https_download",
        "archive_sha256": RELEASE.sha256,
        "archive_size_bytes": RELEASE.byte_size,
        "authority": asdict(authority),
        "model_calls": 0,
    }
    if load:
        subprocess.run(["docker", "load", "--input", str(archive)], check=True, timeout=300)
        inspected = json.loads(
            subprocess.check_output(
                ["docker", "image", "inspect", RELEASE.oci_index_id], text=True, timeout=30
            )
        )[0]
        if (
            inspected["Id"] != RELEASE.oci_index_id
            or inspected.get("Os") != "linux"
            or inspected.get("Architecture") != "amd64"
            or inspected["Config"].get("Labels", {}).get("io.openevo.managed-runtime") != "true"
        ):
            raise ValueError("loaded runtime identity/platform/label differs")
        smoke = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "/opt/codex/bin/codex",
                RELEASE.oci_index_id,
                "--version",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        report.update(
            status="READY", startup_verified=True, codex_cli_version=smoke.stdout.strip()
        )
    return report
