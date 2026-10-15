#!/usr/bin/env python3
"""Offline evidence for the pinned Codex V8 math path; not a general license scanner.

Requires Python 3.11+, pyelftools==0.32 and GNU binutils. Inputs are the
runtime-native-link-materials companion and the five original native executables.
Only relocation operand bytes are ignored when comparing whole ELF functions.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import subprocess
import tarfile
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection

PINS = {
    "librusty_v8_release_x86_64-unknown-linux-musl.a.gz": "232a860a60790dc59af99e360e1f3b2ba882c8a1aaaeb055a2a482eddbce8c70",
    "codex": "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902",
    "codex-code-mode-host": "107cc233a8d90a545ee9647c527c1161068512880f1ee4eda2415eb7d33a700b",
    "bwrap": "77360cb751ccedc5971391444ac86a8a33c15b04d6b4a6fe45f5d25496e62c4c",
    "rg": "ebeaf56f8a25e102e9419933423738b3a2a613a444fd749d695e15eba53f71f2",
    "zsh": "67faaaa89242c4a332e16e508a1977cffc24bf7fca31d4411cdfd101f3831ef3",
}

SOURCE_PINS = {
    "v8-14.9.207.2.tar.gz": "b5f95b644e6caea6a5ea578cc498cafd942dc2917de8e30627a6af77996271ee",
    "codex-rust-v0.144.1-source.tar.gz": "c11868501ef0e67c90759deaf2c0933cd07e4bea4f47a47186ee6de046c57ef6",
}
ICU_SHA = "1cf67874b5a87a8363a86fb3f81e3cbbed54d389062dab8fb52308d5cf8c8612"
ICU_REVISION = "a86a32e67b8d1384b33f8fa48c83a6079b86f8cd"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_pinned(path: Path) -> bytes:
    data = path.read_bytes()
    if sha(data) != PINS[path.name]:
        raise ValueError(f"Pinned hash mismatch: {path.name}")
    return data


def inspect_binary(data: bytes) -> dict:
    elf = ELFFile(io.BytesIO(data))
    dynamic = elf.get_section_by_name(".dynamic")
    needed = [tag.needed for tag in dynamic.iter_tags("DT_NEEDED")] if dynamic else []
    executable_ranges = [
        (s["p_offset"], s["p_offset"] + s["p_filesz"])
        for s in elf.iter_segments()
        if s["p_type"] == "PT_LOAD" and s["p_flags"] & 1
    ]
    return {
        "sha256": sha(data),
        "bytes": len(data),
        "needed": needed,
        "machine": elf["e_machine"],
        "executable_file_ranges": executable_ranges,
    }


def function_matches(obj: bytes, binaries: dict[str, bytes]) -> list[dict]:
    elf = ELFFile(io.BytesIO(obj))
    if elf["e_machine"] != "EM_X86_64" or elf["e_type"] != "ET_REL":
        raise ValueError("Expected an x86_64 relocatable object")
    rows = []
    for sym in elf.get_section_by_name(".symtab").iter_symbols():
        if sym["st_info"]["type"] != "STT_FUNC" or sym["st_size"] < 64:
            continue
        section_id, start, size = sym["st_shndx"], sym["st_value"], sym["st_size"]
        body = elf.get_section(section_id).data()[start : start + size]
        mask = bytearray(b"\xff" * size)
        relocations = []
        for section in elf.iter_sections():
            if not isinstance(section, RelocationSection) or section["sh_info"] != section_id:
                continue
            for relocation in section.iter_relocations():
                offset = relocation["r_offset"] - start
                if not 0 <= offset < size:
                    continue
                kind = relocation["r_info_type"]
                # Fail on unknown relocation types, rather than masking guessed bytes.
                width = {1: 8, 2: 4, 4: 4, 9: 4, 10: 4, 11: 4, 24: 8, 41: 4, 42: 4}[kind]
                if offset + width > size:
                    raise ValueError("Relocation extends outside function")
                mask[offset : offset + width] = b"\0" * width
                relocations.append({"offset": offset, "type": kind, "width": width})
        spans, begin = [], 0
        for index, fixed in enumerate(mask + b"\0"):
            if not fixed:
                if index > begin:
                    spans.append((begin, index))
                begin = index + 1
        a, b = max(spans, key=lambda span: span[1] - span[0])
        if b - a < 16:
            raise ValueError("Insufficient fixed-byte anchor")
        matches = {}
        for name, data in binaries.items():
            ranges = inspect_binary(data)["executable_file_ranges"]
            found, cursor = [], 0
            while (position := data.find(body[a:b], cursor)) >= 0:
                cursor, candidate = position + 1, position - a
                if not any(lo <= candidate and candidate + size <= hi for lo, hi in ranges):
                    continue
                if all(data[candidate + i] == body[i] for i, fixed in enumerate(mask) if fixed):
                    found.append(candidate)
            matches[name] = found
        rows.append(
            {
                "symbol": sym.name,
                "size": size,
                "fixed_bytes": mask.count(255),
                "object_body_sha256": sha(body),
                "relocations": relocations,
                "matches": matches,
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materials", type=Path, required=True)
    parser.add_argument("--binaries", type=Path, required=True)
    parser.add_argument("--codex-source", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True, help="New, empty output directory")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=False)
    for path in (args.materials / "v8-14.9.207.2.tar.gz", args.codex_source):
        if sha(path.read_bytes()) != SOURCE_PINS[path.name]:
            raise ValueError(f"Source hash mismatch: {path.name}")
        with tarfile.open(path) as bundle:
            bundle.extractall(args.work / "source", filter="data")
    source = args.work / "source/v8-14.9.207.2"
    codex_source = args.work / "source/codex-rust-v0.144.1"
    license_files = [source / name for name in ("LICENSE", "LICENSE.v8", "LICENSE.fdlibm")]
    license_files += [source / name for name in ("src/base/ieee754.cc", "src/base/ieee754.h")]
    license_files += sorted((source / "third_party/glibc").rglob("*"))
    source_inventory = [
        {
            "path": str(p.relative_to(source)),
            "sha256": sha(p.read_bytes()),
            "bytes": p.stat().st_size,
        }
        for p in license_files
        if p.is_file()
    ]
    patches = []
    for name in ("v8_module_deps.patch", "v8_bazel_rules.patch", "v8_source_portability.patch"):
        path = codex_source / "patches" / name
        result = subprocess.run(
            ["patch", "-p3", "-i", str(path.resolve())],
            cwd=source,
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        patches.append(
            {
                "path": "patches/" + name,
                "sha256": sha(path.read_bytes()),
                "exit_code": result.returncode,
            }
        )
    bazel_files = [source / "BUILD.bazel", *sorted((source / "bazel").rglob("*.bzl"))]
    forbidden_build_inputs = [
        str(p.relative_to(source))
        for p in bazel_files
        if any(
            token in p.read_text()
            for token in ("V8_USE_LIBM_TRIG_FUNCTIONS", "third_party/glibc/")
        )
    ]
    library = args.work / "librusty_v8.a"
    library.write_bytes(
        gzip.decompress(
            read_pinned(args.materials / "librusty_v8_release_x86_64-unknown-linux-musl.a.gz")
        )
    )
    members = subprocess.check_output(["ar", "t", str(library)], text=True).splitlines()
    symbols = subprocess.check_output(["nm", "-A", str(library)], text=True)
    (args.work / "archive-members.txt").write_text("\n".join(members) + "\n")
    (args.work / "archive-symbols.txt").write_text(symbols)
    forbidden = [
        token
        for token in ("glibc_sin", "glibc_cos", "__branred", "__sincostab")
        if token in symbols
    ]
    forbidden_members = [
        name
        for name in members
        if any(token in name for token in ("s_sin.", "branred.", "sincostab."))
    ]
    # Both src/numbers and src/base objects have this basename. Select by symbols,
    # retaining the duplicate-member ordinal rather than silently overwriting one.
    candidates = []
    for ordinal in range(1, members.count("ieee754.pic.o") + 1):
        subprocess.run(
            ["ar", "xN", str(ordinal), str(library.resolve()), "ieee754.pic.o"],
            cwd=args.work,
            check=True,
        )
        obj = (args.work / "ieee754.pic.o").read_bytes()
        object_symbols = subprocess.check_output(
            ["nm", str(args.work / "ieee754.pic.o")], text=True
        )
        if "_ZN2v84base7ieee7543sinEd" in object_symbols:
            candidates.append((ordinal, obj))
    if len(candidates) != 1:
        raise ValueError("Expected exactly one fdlibm math object")
    ordinal, obj = candidates[0]
    (args.work / "fdlibm.o").write_bytes(obj)
    # A small source-build/link check, explicitly not a full V8 or Codex rebuild.
    # GCC needs the no-warning-probe definition for this V8 header revision.
    harness = args.work / "math-smoke.cc"
    harness.write_text(
        "#include <cstdio>\nnamespace v8::base::ieee754 { double sin(double); double cos(double); }\n"
        'int main() { std::printf("%.17g %.17g\\n", v8::base::ieee754::sin(1), '
        "v8::base::ieee754::cos(1)); }\n"
    )
    compile_flags = ["-std=c++20", "-O2", "-DNDEBUG", "-D__has_warning(x)=0"]
    subprocess.run(
        [
            "g++",
            *compile_flags,
            "-I",
            str(source),
            "-c",
            str(source / "src/base/ieee754.cc"),
            "-o",
            str(args.work / "fdlibm-from-source.o"),
        ],
        check=True,
        timeout=60,
    )
    math_smoke = {}
    for name in ("fdlibm", "fdlibm-from-source"):
        executable = args.work / (name + "-smoke")
        subprocess.run(
            ["g++", str(harness), str(args.work / (name + ".o")), "-lm", "-o", str(executable)],
            check=True,
            timeout=60,
        )
        output = subprocess.check_output(
            [str(executable.resolve())], text=True, timeout=10
        ).strip()
        values = [float(value) for value in output.split()]
        if len(values) != 2 or not all(
            math.isclose(a, b, rel_tol=0, abs_tol=1e-14)
            for a, b in zip(values, (math.sin(1), math.cos(1)))
        ):
            raise ValueError("fdlibm source/object smoke mismatch")
        math_smoke[name] = {"stdout": output, "executable_sha256": sha(executable.read_bytes())}
    binaries = {
        name: read_pinned(args.binaries / name) for name in PINS if not name.endswith(".gz")
    }
    icu_data = (args.materials / f"icu-{ICU_REVISION}-common_icudtl.dat").read_bytes()
    if sha(icu_data) != ICU_SHA:
        raise ValueError("Pinned ICU data mismatch")
    icu_offsets = {n: binaries[n].find(icu_data) for n in ("codex", "codex-code-mode-host")}
    rows = function_matches(obj, {n: binaries[n] for n in ("codex", "codex-code-mode-host")})
    passed = (
        not forbidden
        and not forbidden_members
        and not forbidden_build_inputs
        and all(offset >= 0 for offset in icu_offsets.values())
        and len(rows) == 21
        and all(len(offsets) == 1 for row in rows for offsets in row["matches"].values())
    )
    report = {
        "status": "PASS_FDLIBM_BINARY_BINDING" if passed else "BLOCKED",
        "scope": "Pinned release math path, not an exhaustive native SBOM or legal opinion",
        "archive_sha256": sha(library.read_bytes()),
        "archive_members": len(members),
        "glibc_symbols_found": forbidden,
        "glibc_objects_found": forbidden_members,
        "math_object_ordinal": ordinal,
        "math_object_sha256": sha(obj),
        "source_archive_hashes": SOURCE_PINS,
        "math_and_license_sources": source_inventory,
        "codex_bazel_patches": patches,
        "forbidden_bazel_inputs": forbidden_build_inputs,
        "patched_bazel_build_sha256": sha((source / "BUILD.bazel").read_bytes()),
        "math_link_smoke": {
            "status": "PASS",
            "results": math_smoke,
            "source_compile_flags": compile_flags,
            "scope": "GNU harness and fdlibm only; not rebuilt Codex/V8",
            "compiler": subprocess.check_output(["g++", "--version"], text=True).splitlines()[0],
        },
        "icu": {
            "revision": ICU_REVISION,
            "data_sha256": ICU_SHA,
            "bytes": len(icu_data),
            "whole_data_file_offsets": icu_offsets,
        },
        "binaries": {n: inspect_binary(b) for n, b in binaries.items()},
        "functions": rows,
    }
    (args.work / "native-link-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "functions_per_binary": len(rows)}))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
