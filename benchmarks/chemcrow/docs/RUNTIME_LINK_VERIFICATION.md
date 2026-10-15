# Original runtime native link verification

Scope: the exact Linux amd64 runtime and Codex 0.144.1 identified in
[RUNTIME_DISTRIBUTION_REVIEW.md](RUNTIME_DISTRIBUTION_REVIEW.md). These commands make no model calls.
They keep source archives, the original OCI and historical experiments unchanged. Local candidate
materials have been tested; release URLs and anonymous public delivery are still pending approval.

## Materials

Keep the entire reviewed release set together, as enumerated by `release-assets.json` and
`SHA256SUMS`. Do not upload just the image and omit its source/notices.

| Material | Purpose |
| --- | --- |
| Original OCI archive | Immutable historical binary; SHA `ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149` |
| `bundled-debian-notices.tar.gz`, `debian-corresponding-sources.tar.gz` | Exact installed Debian copyright/terms and 104 complete source versions with packaging/build inputs |
| `Codex-0.144.1-LICENSE`, `Codex-0.144.1-NOTICE`, `codex-rust-v0.144.1-source.tar.gz` | Exact application source, original wrapper, native vendor files, patches and upstream workflows |
| `codex-locked-dependencies.tar.gz`, `codex-additional-notices.tar.gz` | Verified registry/Git source closure and supplementary notices; intentionally overinclusive |
| `runtime-distribution-supplements.tar.gz` | **Unchanged** 52,831,506-byte companion, SHA `d008203723b3a4c2947beb03d321f9b3e9b5fcfc9513ed6d139b1e58ef79dd79`; native/pip sources, 110 notice mappings, compiler notices, bwrap patch/adapter/lock |
| `runtime-native-link-materials.tar.gz` | Actual Bazel V8 source archive, verified release `.a.gz`/binding/manifest, exact ICU data/notices, Bazel dependency notices, input provenance and executable verification scripts |
| `THIRD_PARTY_NOTICES.md`, manifests, source integrity/origin/link/relink/smoke receipts | Notice entry point, exact hashes, modification and verification record |

No original application object files are fabricated. No full Codex or V8 rebuild is claimed. Actual
LGPL bubblewrap can be recompiled and relinked from supplied sources and replaced through supported
Codex discovery. The excluded V8/glibc math path does not create a missing Codex relink requirement.
V8 source still contains the LGPL files and retains their licenses in the source archive.

## Isolated prerequisites

Use Linux x86_64, Docker, Python 3.11+, gcc/g++/ar/make, Perl, patch and strace. On Debian/Ubuntu,
`build-essential binutils perl patch strace python3-venv` supply the host build tools. The scripts
require new work directories and verify the exact source/binary pins before using them. Install the
ELF reader in a dedicated environment, for example:

```bash
python3.11 -m venv /path/to/native-audit-venv
/path/to/native-audit-venv/bin/python -m pip install pyelftools==0.32
```

Prepare Rust **1.95.0** in an isolated prefix; no system Rust replacement is needed. Download these
three files from `https://static.rust-lang.org/dist/` and verify these SHA-256 values:

```text
8426a3d170a5879f5682f5fbdd024a1779b3951e7baba685af2d6dc32a6dfc15  rustc-1.95.0-x86_64-unknown-linux-gnu.tar.xz
e74edd2cf7d0f1f1383b4f00eb90c843750bc489e2ccf7214e6476678a907425  cargo-1.95.0-x86_64-unknown-linux-gnu.tar.xz
047ea7098803d3500fa1072e9cee5392697e21525559e4458128a2bf874aa382  rust-std-1.95.0-x86_64-unknown-linux-gnu.tar.xz
```

Extract each verified archive in a separate tools directory and run its `install.sh` with
`--prefix=/path/to/isolated-rust --disable-ldconfig`. Upstream compiler licenses stay in that
installation; the corresponding release copyright texts are already included in the notice
companion. Cargo compilation below uses an empty cache, `--offline --locked`, a local checksum-
verified libc crate and an allowlisted environment without inherited API credentials.

## Commands from a clean candidate checkout

Until publication, `ASSETS` is a copy of the locally staged, reviewed assets. After publication,
obtain the same files from the **actual approved release**, and verify `SHA256SUMS` against the
reviewed source commit's `release-assets.json`. Do not treat a local copy as anonymous download.
All paths below are absolute, user-selected locations; `AUDIT_ROOT` must initially be absent.

```bash
ASSETS=/path/to/reviewed-release-assets
AUDIT_ROOT=/path/to/new-native-audit
AUDIT_PYTHON=/path/to/native-audit-venv/bin/python
RUST_PREFIX=/path/to/isolated-rust
mkdir -p "$AUDIT_ROOT/materials" "$AUDIT_ROOT/binaries"
(cd "$ASSETS" && sha256sum -c SHA256SUMS)
tar -xzf "$ASSETS/runtime-native-link-materials.tar.gz" -C "$AUDIT_ROOT/materials"
docker load --input "$ASSETS/openevo-science-runtime-0.1.1-linux-amd64.tar.gz"
```

Extract native binaries from the exact loaded image without running them or mounting credentials:

```bash
runtime_image=sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b
runtime_vendor=/opt/codex/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl
extract_container=$(docker create --entrypoint /bin/true "$runtime_image")
for relative in bin/codex bin/codex-code-mode-host codex-resources/bwrap codex-path/rg codex-resources/zsh/bin/zsh; do
  docker cp "$extract_container:$runtime_vendor/$relative" "$AUDIT_ROOT/binaries/$(basename "$relative")"
done
docker rm "$extract_container"
```

Run the source/object/ELF audit, clean LGPL relink and original-image smoke:

```bash
"$AUDIT_PYTHON" benchmarks/chemcrow/scripts/audit_runtime_native_links.py \
  --materials "$AUDIT_ROOT/materials" --binaries "$AUDIT_ROOT/binaries" \
  --codex-source "$ASSETS/codex-rust-v0.144.1-source.tar.gz" \
  --work "$AUDIT_ROOT/native-check"
"$AUDIT_PYTHON" benchmarks/chemcrow/scripts/verify_runtime_bwrap_relink.py \
  --assets "$ASSETS" --toolchain "$RUST_PREFIX" --codex "$AUDIT_ROOT/binaries/codex" \
  --work "$AUDIT_ROOT/relink-check"
"$AUDIT_PYTHON" benchmarks/chemcrow/scripts/smoke_runtime_native.py \
  --archive "$ASSETS/openevo-science-runtime-0.1.1-linux-amd64.tar.gz" \
  --modified-bwrap "$AUDIT_ROOT/relink-check/target/release/bwrap" \
  --output "$AUDIT_ROOT/original-image-native-smoke.json"
```

Expected receipts: `PASS_FDLIBM_BINARY_BINDING` (21 functions in each binary),
`PASS_CLEAN_MODIFIED_LIBRARY_RELINK` with `PASS_ORIGINAL_CODEX_USES_MODIFIED_BWRAP`, and
`PASS_ORIGINAL_IMAGE_NATIVE_SMOKE`. Expected JavaScript values are sin(1)=0.8414709848078965,
cos(1)=0.5403023058681398, German formatting `1.234,5`. The archive hash is checked before and after.
The independently observed execution record is
[native-independent-acceptance.json](../reports/chemcrowrun_acceptance_20261015/native-independent-acceptance.json).

The bwrap verification patch only changes its version banner and adds a dated modification notice.
The original Rust wrapper remains unchanged; the standalone Cargo adapter substitutes explicit
native compile/archive/link commands for its workspace build script. Objects, `libbwrap_modified.a`,
`libcap.a`, final executable and build log stay in the new work directory. The result is a GNU/Linux
verification build, not an identical rebuild of the release musl executable.

To use the modified bwrap, prepend its `target/release` directory to PATH when launching original
Codex from a directory **outside** that replacement directory. The provided test does this with an
isolated HOME and records successful `execve` calls; it does not overwrite the bundled file or weaken
its digest check. If user namespaces or tracing are prohibited by the host, the replacement test
fails explicitly; rerun on a host that permits them. Do not record a skipped sandbox test as PASS.

## Limits and fallback

The optional bundled zsh still needs GLIBC_2.38 and does not start in the historical glibc 2.36 image.
It has not been silently repaired. Shell execution in the scientific run used a working path.

No private upstream build logs or signed full-binary build attestation were available. The evidence
binds the relevant unstripped release objects to the shipped code, rather than claiming a full
native SBOM or byte-reproducible toolchain build. Full V8/Codex rebuilding was not required to close
the corrected math finding and remains **NOT PERFORMED**.

If a later release changes the pins or re-enables LGPL native code, stop automatic clearance and
supply/test its corresponding sources and linkable application before distribution. A fallback can
fetch Codex's pinned official npm package independently, or build a new runtime from public source.
Such a runtime must get a new image/config identity and a separate experiment variant; functional
similarity must never be labeled as the original historical image. No replacement image was needed
or built for this correction. Remaining work is approved publication and real remote fresh-clone
paid reproduction, with new run state and budget protection.
