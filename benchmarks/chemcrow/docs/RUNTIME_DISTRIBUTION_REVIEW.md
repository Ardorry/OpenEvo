# Original runtime distribution review

**Review completed with a distribution blocker: `V8-LGPL-RELINK`.** The original image must remain
withheld from publication. This is a concrete source/linking evidence gap, not a finding that the
upstream distribution violates a license. User publication approval and anonymous download acceptance
are separate gates and have not occurred.

This review concerns the historical OCI archive, SHA-256
`ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149`.
Its bytes, image identity and historical experiments are unchanged. No model calls were made for this
review. The [decision and evidence](../reports/chemcrowrun_acceptance_20261015/runtime-distribution-review.json)
record what passed and what still prevents release.

## Completed checks and materials

| Component | Verified evidence | Distribution handling |
| --- | --- | --- |
| Original OCI | Ten-layer bounded scan, 17,480 members; config/history scan; original archive hash retained | No prescribed sensitive paths or high-confidence credential patterns found; bounded scanning is not proof that every possible secret is absent |
| Debian | 104 exact source versions; all 223 source files match their DSC SHA-256 and lengths | Keep copyright/license files and complete Debian source companions, including packaging/build inputs, available with the binary; this check does not independently validate DSC signatures |
| Codex 0.144.1 | Official npm package integrity verified; all five native executables and package-layout manifest exactly match the installed image | Retain exact Codex LICENSE/NOTICE and separate native/dependency terms; npm's Apache declaration does not override them |
| ripgrep 15.1.0 | Binary matches upstream release and Codex's pinned archive hash; PCRE2 10.45 reported by the binary | MIT/Unlicense choice for ripgrep; preserve PCRE2 and dependency notices separately |
| Patched zsh | Binary matches `codex-zsh-v0.1.0`; source commit `77045ef899e53b9598bebc5a41db93a548a40ca6`, Codex patch and build script obtained | Retain zsh copyright/license and upstream modification provenance; do not treat optional GPL shell functions in the source archive as evidence that they are installed |
| bubblewrap | Vendored source identifies 0.11.2 and LGPL-2.0-or-later; Rust wrapper/build files retained; libcap 2.75 source matches the upstream build pin | Provide library and wrapper sources, build instructions and LGPL notices; modified-library relink verification passed |
| pip 25.0.1 | All 431 installed non-cache source files exactly match the upstream sdist | Restore omitted vendored notices from that sdist; retain its actual vendored source, including MPL-covered certifi, and original notices |
| Python 3.12.13, Node 22.23.1, npm/corepack | Installed inventories and bundled licenses retained; Python source obtained; npm dependency metadata enumerated | Preserve component notices; parent package declarations do not replace vendored attribution |
| Rust / compiler runtimes | Exact release compiler copyright documents obtained from checksum-verified Rust 1.95.0 and the 2025-10-22 nightly archives | Preserve standard-library/native notices; collection is overinclusive, not a precise link map |
| Cargo registry and Git sources | 1193 registry checksums and seven exact Git source repositories retained | Keep original metadata, source headers and license choices; full workspace lock is not a linked Linux SBOM |
| V8 / ICU / native code | 23 license files from the exact V8 tree, 22 submodule-root notices, source archives and compiler notices obtained | **Blocked:** actual native applicability and complete linkable-work evidence are not yet established |

Receipts: [Debian source integrity](../reports/chemcrowrun_acceptance_20261015/debian-source-integrity-review.json),
[npm binary origin](../reports/chemcrowrun_acceptance_20261015/codex-npm-origin-verification.json),
[pip source comparison](../reports/chemcrowrun_acceptance_20261015/pip-source-comparison.json),
[native inventory](../reports/chemcrowrun_acceptance_20261015/runtime-native-inventory.json), and
[bubblewrap relink](../reports/chemcrowrun_acceptance_20261015/bubblewrap-relink-verification.json).

The bubblewrap test compiles the supplied C sources, libcap and original Rust wrapper with a dated
version-banner modification. It uses an isolated Cargo manifest and local libc source, with Cargo
network access disabled. The resulting GNU/Linux binary prints the modified banner. It demonstrates
relinking for that component; it is not the original musl executable, a replacement runtime, or a V8
relink test. There is no requirement here to reproduce historical image bytes by rebuilding.

## Disposition of the 43 filename flags

The [per-package worksheet](../reports/chemcrowrun_acceptance_20261015/runtime-license-candidate-dispositions.json)
records every earlier candidate, its declared terms, selected permissive alternative where available,
actual source-header locations and exact-tree notices. It distinguishes an upstream license file,
embedded permission text and package metadata. All original source archives and notices are retained;
no copyright holder or year has been invented.

A missing standalone LICENSE filename is not itself a licensing violation. Conversely, a package
SPDX field does not establish the license of embedded third-party data or native code. In particular,
`deno_core_icudata`'s MIT wrapper declaration does not replace ICU/Unicode attribution. The Rust
`option-ext` MPL source is included; `r-efi` and `self_cell` have permissive OR alternatives, so their
LGPL/GPL alternatives are not automatically mandatory. AND expressions retain all applicable notices.

A conservative lock-graph traversal from `codex-cli`, `codex-code-mode-host` and `codex-bwrap` finds no
path to LiveKit, soxr or webrtc-sys. Their sources remain in the overinclusive companion, but this
review does not claim that they are linked into the image. Merged development/platform edges in the
lock graph prevent it from being an exact native SBOM.

## Remaining blocker: V8-LGPL-RELINK

The exact `v8-149.2.0` source contains glibc math code under LGPL-2.1-or-later. In `v8/gni/v8.gni`,
`v8_use_libm_trig_functions` defaults to `is_clang`; `v8/BUILD.gn` conditionally compiles those files.
The installed executables have Clang compiler comments. This combination warrants checking the
actual release build inputs; compiler comments and stripped symbol tables do not establish whether
that particular source set was linked.

The [LGPL terms supplied with bubblewrap](https://raw.githubusercontent.com/openai/codex/rust-v0.144.1/codex-rs/vendor/bubblewrap/COPYING)
distinguish supplying library source from supplying the work needed to modify and relink a combined
executable. Applicable V8/glibc terms must be handled for that component too. Merely collecting crate
archives or passing the separate bubblewrap test does not verify the complete Codex/code-mode-host
source/build closure. This review has not performed a full V8/Codex relink build.

Close this gate by:

1. Binding actual V8 release build options or object provenance to the shipped package and determining
   whether its LGPL math path was included. Do not assume that the absence of readable symbols proves
   exclusion.
2. If included or not excluded, providing and validating the complete corresponding source/build inputs
   or relinkable work for the affected executables, with modification/relink instructions and notices.
   A functional relink check is an engineering verification method, not a claim that a license requires
   identical-byte rebuilding.
3. Completing native and ICU data attribution against that verified closure. An unrelated source tree
   or current-main license is only supplementary evidence, never an exact historical match.

No upstream maintainer has been contacted, and no separate legal clearance has been represented as
obtained. There is no blanket Apache-2.0 license for the assembled image.

## Runtime limitation found during review

The bundled zsh fails to start in the original Debian 12 image because it requires `GLIBC_2.38`, while
the image has glibc 2.36. Record this as a limitation of that optional bundled executable. Codex and
the actual experiment's shell path already ran successfully. Do not silently replace zsh or upgrade
libraries inside an image still described as the original historical runtime.

## Release boundary

Staging contains `THIRD_PARTY_NOTICES.md`, supplementary source/notice material, provenance receipts,
`runtime-distribution-review.json`, the release inventory and SHA256SUMS. Prior review manifests are
preserved separately. Candidate assets remain **not authorized for distribution**; scans, hashes and
local builds do not change that status.

If the blocker is closed and publication is approved, publish the required notices and corresponding
sources alongside the binary, with explicit source-availability instructions. Retain third-party
terms and recipients' modification/relink rights. Then verify the real public URL anonymously,
including hash/import/start checks and a remote fresh clone. Local staging is not that acceptance.

The ChemCrow notebook fixture has no discovered redistribution license at its locked commit. It is
fetched from public upstream and is not a release asset. RXN source/weights are fetched from their
locked upstream sources; rebuilt RXN containers remain distinct from this historical image.
