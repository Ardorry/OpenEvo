#!/usr/bin/env python3
"""No-model, network-disabled original-image Codex and code-mode-host smoke tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import select
import struct
import subprocess
import time
import uuid
from pathlib import Path

IMAGE = "sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b"
ARCHIVE_SHA = "ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149"
PREFIX = (
    "/opt/codex/lib/node_modules/@openai/codex/node_modules/@openai/"
    "codex-linux-x64/vendor/x86_64-unknown-linux-musl"
)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--modified-bwrap", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    before = digest(args.archive)
    if before != ARCHIVE_SHA:
        raise ValueError("Original archive mismatch")
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], text=True
    ).strip()
    if image_id != IMAGE:
        raise ValueError("Loaded image identity mismatch")
    base = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--env",
        "HOME=/tmp/native-smoke",
        "--env",
        "CODEX_HOME=/tmp/native-smoke/codex",
    ]
    codex = subprocess.run(
        base + ["--entrypoint", PREFIX + "/bin/codex", IMAGE, "--version"],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    if codex.stdout.strip() != "codex-cli 0.144.1":
        raise ValueError("Unexpected Codex version")
    bwrap = subprocess.run(
        base
        + [
            "--mount",
            f"type=bind,src={args.modified_bwrap.resolve()},dst=/audit/bwrap,readonly",
            "--entrypoint",
            "/audit/bwrap",
            IMAGE,
            "--version",
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    if "ChemCrowRun relink verification" not in bwrap.stdout:
        raise ValueError("Modified bwrap did not start in the original image")
    name = "chemcrowrun-native-smoke-" + uuid.uuid4().hex[:12]
    process = subprocess.Popen(
        base + ["--name", name, "-i", "--entrypoint", PREFIX + "/bin/codex-code-mode-host", IMAGE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    frames = []
    deadline = time.monotonic() + 30

    def read_bytes(length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            timeout = deadline - time.monotonic()
            if timeout <= 0 or not select.select([process.stdout], [], [], timeout)[0]:
                raise TimeoutError("code-mode-host response timeout")
            chunk = os.read(process.stdout.fileno(), length - len(data))
            if not chunk:
                raise ValueError("Unexpected code-mode-host EOF")
            data.extend(chunk)
        return bytes(data)

    def receive() -> dict:
        size = struct.unpack("<I", read_bytes(4))[0]
        if size > 1024 * 1024:
            raise ValueError("Unexpectedly large smoke response")
        frame = json.loads(read_bytes(size))
        frames.append(frame)
        return frame

    def send(message: dict) -> None:
        data = json.dumps(message).encode()
        process.stdin.write(struct.pack("<I", len(data)) + data)
        process.stdin.flush()

    def request(number: int, method: str, **extra: object) -> None:
        send(
            {
                "type": "operation/request",
                "id": number,
                "request": {"method": method, "sessionId": "native-smoke", **extra},
            }
        )

    try:
        send(
            {
                "type": "connection/hello",
                "supportedVersions": [1],
                "requiredCapabilities": [],
                "optionalCapabilities": [],
            }
        )
        if receive()["type"] != "connection/ready":
            raise ValueError("Host handshake failed")
        request(1, "session/open")
        opened = receive()
        if opened["result"]["value"]["type"] != "session/ready":
            raise ValueError("Host session failed")
        request(
            2,
            "session/execute",
            request={
                "tool_call_id": "native-smoke",
                "enabled_tools": [],
                "yield_time_ms": 1000,
                "max_output_tokens": 512,
                "source": "text(JSON.stringify({sin:Math.sin(1),cos:Math.cos(1),"
                'locale:new Intl.NumberFormat("de-DE").format(1234.5)}));',
            },
        )
        result = None
        for _ in range(10):
            frame = receive()
            if frame["type"] == "execute/initialResponse":
                result = frame["result"]["value"]["Result"]
                break
        if result is None or result["error_text"] is not None:
            raise ValueError("V8 execution failed")
        values = json.loads(result["content_items"][0]["text"])
        if (
            not math.isclose(values["sin"], math.sin(1), abs_tol=1e-14)
            or not math.isclose(values["cos"], math.cos(1), abs_tol=1e-14)
            or values["locale"] != "1.234,5"
        ):
            raise ValueError("Unexpected V8 math or ICU result")
        request(3, "session/shutdown")
        for _ in range(10):
            frame = receive()
            if frame.get("id") == 3 and frame["type"] == "operation/response":
                if frame["result"]["value"]["type"] != "session/closed":
                    raise ValueError("Session shutdown failed")
                break
        else:
            raise ValueError("No shutdown acknowledgment")
        process.stdin.close()
        if process.wait(timeout=10) != 0:
            raise ValueError("Host exited unsuccessfully")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        subprocess.run(
            ["docker", "rm", "-f", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    after = digest(args.archive)
    if after != before:
        raise ValueError("Original archive changed")
    report = {
        "status": "PASS_ORIGINAL_IMAGE_NATIVE_SMOKE",
        "image": IMAGE,
        "archive_sha256_before": before,
        "archive_sha256_after": after,
        "paid_calls": 0,
        "container_network": "none",
        "root_filesystem": "read-only",
        "credentials_mounted": False,
        "codex_version": codex.stdout.strip(),
        "modified_bwrap_version": bwrap.stdout.strip(),
        "modified_bwrap_sha256": digest(args.modified_bwrap),
        "code_mode_host": {"frames": frames, "observed_values": values},
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "values": values}))


if __name__ == "__main__":
    main()
