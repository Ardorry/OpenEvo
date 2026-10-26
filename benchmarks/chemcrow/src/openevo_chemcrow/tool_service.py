from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import AbstractContextManager
from pathlib import Path


class PairToolService(AbstractContextManager[str]):
    def __init__(
        self,
        *,
        pair_id: str,
        cache_root: Path,
        controlled_chemicals_csv: Path,
        log_path: Path,
        network_enabled: bool,
        bind_host: str = "0.0.0.0",
        advertise_host: str = "host.docker.internal",
    ) -> None:
        self.pair_id = pair_id
        self.cache_root = cache_root
        self.controlled_chemicals_csv = controlled_chemicals_csv
        self.log_path = log_path
        self.network_enabled = network_enabled
        self.bind_host = bind_host
        self.advertise_host = advertise_host
        self.port = _free_port(bind_host)
        self.process: subprocess.Popen[bytes] | None = None
        self._log_handle = None

    def __enter__(self) -> str:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("ab")
        command = [
            sys.executable,
            "-m",
            "openevo_chemcrow.mcp_server",
            "--pair-id",
            self.pair_id,
            "--cache-root",
            str(self.cache_root),
            "--controlled-chemicals-csv",
            str(self.controlled_chemicals_csv),
            "--host",
            self.bind_host,
            "--port",
            str(self.port),
        ]
        if not self.network_enabled:
            command.append("--disable-network")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            env=dict(os.environ),
        )
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("pair-scoped ChemCrow MCP service exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.25):
                    return f"http://{self.advertise_host}:{self.port}/mcp"
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("pair-scoped ChemCrow MCP service did not become reachable")

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self._log_handle is not None:
            self._log_handle.close()


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
