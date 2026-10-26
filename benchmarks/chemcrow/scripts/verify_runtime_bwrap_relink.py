#!/usr/bin/env python3
"""Build modified LGPL bubblewrap from pinned release companions in a new directory.

No network or model calls. Needs GNU gcc/ar/make, Perl, patch, and Rust/Cargo
1.95.0. Does not alter the original binaries, image, sources or installed tools.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

PINS = {
    "codex-rust-v0.144.1-source.tar.gz": "c11868501ef0e67c90759deaf2c0933cd07e4bea4f47a47186ee6de046c57ef6",
    "codex-locked-dependencies.tar.gz": "029746f0dd1c211e889a10dfead1058ecb7d08f2e881b2c1a84a169a479c19ae",
    "runtime-distribution-supplements.tar.gz": "d008203723b3a4c2947beb03d321f9b3e9b5fcfc9513ed6d139b1e58ef79dd79",
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def extract_selected(archive: Path, destination: Path, prefixes: tuple[str, ...]) -> None:
    with tarfile.open(archive) as bundle:
        members = [m for m in bundle if any(m.name.startswith(p) for p in prefixes)]
        if not members:
            raise ValueError(f"Required members missing from {archive.name}")
        bundle.extractall(destination, members=members, filter="data")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument(
        "--toolchain", type=Path, required=True, help="Rust 1.95.0 installation prefix"
    )
    parser.add_argument(
        "--codex", type=Path, help="Original Codex; also verify replacement via strace"
    )
    parser.add_argument("--work", type=Path, required=True, help="Must not already exist")
    args = parser.parse_args()
    work, assets, toolchain = args.work.resolve(), args.assets.resolve(), args.toolchain.resolve()
    for name, expected in PINS.items():
        if digest(assets / name) != expected:
            raise ValueError(f"Pinned source companion mismatch: {name}")
    work.mkdir(parents=True, exist_ok=False)
    home = work / "home"
    home.mkdir()
    env = {
        "PATH": f"{toolchain / 'bin'}:/usr/bin:/bin",
        "HOME": str(home),
        "CARGO_HOME": str(work / "cargo-home"),
        "CARGO_TARGET_DIR": str(work / "target"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    commands = []

    def run(command: list[str], cwd: Path = work) -> str:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
        )
        sanitized = [
            s.replace(str(work), "<work>").replace(str(toolchain), "<toolchain>") for s in command
        ]
        if args.codex:
            sanitized = [
                s.replace(str(args.codex.resolve()), "<original-codex>") for s in sanitized
            ]
        commands.append({"command": sanitized, "exit_code": completed.returncode})
        with (work / "build.log").open("a") as log:
            log.write(completed.stdout)
        completed.check_returncode()
        return completed.stdout

    version = run([str(toolchain / "bin/rustc"), "--version"]).strip()
    if not version.startswith("rustc 1.95.0 "):
        raise ValueError("Verification requires the recorded Rust 1.95.0 toolchain")
    extract_selected(
        assets / "codex-rust-v0.144.1-source.tar.gz",
        work / "unpack",
        ("codex-rust-v0.144.1/codex-rs/bwrap/", "codex-rust-v0.144.1/codex-rs/vendor/bubblewrap/"),
    )
    extract_selected(
        assets / "runtime-distribution-supplements.tar.gz",
        work / "unpack",
        ("license-followup/libcap-2.75.tar.xz", "license-followup/relink-check/"),
    )
    extract_selected(
        assets / "codex-locked-dependencies.tar.gz",
        work / "unpack",
        ("codex-locked-crates/libc-0.2.186.crate",),
    )
    source = work / "source"
    source.mkdir()
    upstream = work / "unpack/codex-rust-v0.144.1/codex-rs"
    shutil.copytree(upstream / "bwrap", source / "wrapper")
    shutil.copytree(upstream / "vendor/bubblewrap", source / "bubblewrap")
    extra = work / "unpack/license-followup"
    if digest(extra / "libcap-2.75.tar.xz") != (
        "de4e7e064c9ba451d5234dd46e897d7c71c96a9ebf9a0c445bc04f4742d83632"
    ):
        raise ValueError("libcap source mismatch")
    extract_selected(extra / "libcap-2.75.tar.xz", source, ("libcap-2.75/",))
    extract_selected(
        work / "unpack/codex-locked-crates/libc-0.2.186.crate", source, ("libc-0.2.186/",)
    )
    for name in ("Cargo.toml", "Cargo.lock"):
        shutil.copy2(extra / "relink-check" / name, source / "wrapper" / name)
    run(
        ["patch", "-p1", "-i", str(extra / "relink-check/version-banner.patch")],
        cwd=source / "bubblewrap",
    )
    run(["make", "-C", str(source / "libcap-2.75/libcap"), "libcap.a"])
    objects = work / "objects"
    objects.mkdir()
    native_sources = ["bubblewrap.c", "bind-mount.c", "network.c", "utils.c"]
    for name in native_sources:
        run(
            [
                "gcc",
                "-O2",
                "-D_GNU_SOURCE",
                "-Dmain=bwrap_main",
                f"-I{source / 'wrapper'}",
                f"-I{source / 'bubblewrap'}",
                f"-I{source / 'libcap-2.75/libcap/include'}",
                f"-I{source / 'libcap-2.75/libcap/include/uapi'}",
                "-c",
                str(source / "bubblewrap" / name),
                "-o",
                str(objects / f"{name}.o"),
            ]
        )
    run(
        [
            "ar",
            "rcs",
            str(objects / "libbwrap_modified.a"),
            *[str(objects / f"{name}.o") for name in native_sources],
        ]
    )
    run(
        [
            "cargo",
            "rustc",
            "--offline",
            "--locked",
            "--release",
            "--bin",
            "bwrap",
            "--",
            "--cfg",
            "bwrap_available",
            "-L",
            f"native={objects}",
            "-L",
            f"native={source / 'libcap-2.75/libcap'}",
            "-l",
            "static=bwrap_modified",
            "-l",
            "static=cap",
        ],
        cwd=source / "wrapper",
    )
    binary = work / "target/release/bwrap"
    banner = run([str(binary), "--version"]).strip()
    expected = "bubblewrap built for Codex (ChemCrowRun relink verification)"
    if banner != expected:
        raise ValueError("Modified library did not supply the observed version banner")
    help_text = run([str(binary), "--help"])
    if not all(flag in help_text for flag in ("--argv0", "--perms")):
        raise ValueError("Replacement lacks capabilities required by Codex")
    report = {
        "status": "PASS_CLEAN_MODIFIED_LIBRARY_RELINK",
        "toolchain": version,
        "source_companions": PINS,
        "commands": commands,
        "version_stdout": banner,
        "binary_sha256": digest(binary),
        "original_main_rs_unchanged": digest(source / "wrapper/src/main.rs")
        == digest(upstream / "bwrap/src/main.rs"),
        "scope": "Independent GNU/Linux bwrap build; not a rebuilt Codex or original musl binary",
        "build_log_sha256": digest(work / "build.log"),
        "environment": "Empty Cargo cache; allowlisted environment; Cargo offline and locked",
    }
    if args.codex:
        codex = args.codex.resolve()
        if digest(codex) != "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902":
            raise ValueError("Original Codex hash mismatch")
        env["PATH"] = f"{binary.parent}:/usr/bin:/bin"
        trace = work / "codex-replacement.trace"
        # The replacement must be outside cwd: Codex deliberately rejects a bwrap
        # discovered inside the current project directory. No bundled hash bypass.
        stdout = run(
            [
                "strace",
                "-f",
                "-e",
                "trace=execve",
                "-o",
                str(trace),
                str(codex),
                "sandbox",
                "--",
                "/bin/sh",
                "-c",
                "printf CHEMCROWRUN_REPLACEMENT_OK",
            ],
            cwd=home,
        )
        trace_text = trace.read_text()
        replacement_execs = [
            line
            for line in trace_text.splitlines()
            if f'execve("{binary}"' in line and line.endswith("= 0")
        ]
        sandbox_execs = [line for line in replacement_execs if '"--help"' not in line]
        if "CHEMCROWRUN_REPLACEMENT_OK" not in stdout or not sandbox_execs:
            raise ValueError("No successful original-Codex execution of the modified bwrap")
        report["replacement"] = {
            "status": "PASS_ORIGINAL_CODEX_USES_MODIFIED_BWRAP",
            "codex_sha256": digest(codex),
            "trace_sha256": digest(trace),
            "successful_replacement_execs": len(replacement_execs),
            "successful_sandbox_execs": len(sandbox_execs),
            "stdout": stdout.strip(),
            "method": "Supported PATH discovery; bundled digest untouched",
        }
        report["build_log_sha256"] = digest(work / "build.log")
    (work / "bubblewrap-clean-relink.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "banner": banner}))


if __name__ == "__main__":
    main()
