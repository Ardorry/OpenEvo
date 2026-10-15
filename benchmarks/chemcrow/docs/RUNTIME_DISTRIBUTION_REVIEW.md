# Original runtime distribution review

**Technical and license evidence now supports redistribution of the original image together with
its required source/notice companions.** The earlier `V8-LGPL-RELINK` evidence gap is closed by the
actual Bazel release path, static archive analysis and positive machine-code matches. Publication is
still **NOT AUTHORIZED**. Anonymous public download and remote fresh-clone acceptance remain pending.
This is a documented engineering/license analysis, not a separate professional legal opinion.

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
| V8 / ICU / native code | Actual Bazel V8 14.9.207.2 source/archive pins; 21 fdlibm functions matched in both original ELFs; entire 10,822,192-byte ICU data matched | V8 BSD-3-Clause, fdlibm SunPro, ICU/Unicode and independent native notices retained; conditional glibc math path excluded for these pinned binaries |

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

## Corrected finding: V8, fdlibm and glibc are different components

The initial review correctly found LGPL source in the V8 source tree, but could not establish its
applicability. Its GN default and Clang-comment rationale was insufficient for this release. Codex
0.144.1's `.github/actions/setup-rusty-v8/action.yml` downloads OpenAI's **Bazel-built**
`rusty-v8-v149.2.0` release pair. `MODULE.bazel` pins V8 **14.9.207.2**, rather than compiling the
crate's bundled denoland V8 revision through GN. Both trees remain available; their identities must
not be conflated.

| Component | Exact identity and license basis | Actual disposition |
| --- | --- | --- |
| V8 engine | `v8/v8` tag `14.9.207.2`; source archive SHA `b5f95b644e6caea6a5ea578cc498cafd942dc2917de8e30627a6af77996271ee`; `LICENSE` / `LICENSE.v8`: BSD-3-Clause | Statically present; retain notices |
| V8 fdlibm adaptation | `src/base/ieee754.cc`; its original Sun permission header and `LICENSE.fdlibm` (SHA `e7115e18444dae09d17f361ddc365fb1d342640fe500796209c63f7c80dfae10`) match SPDX **SunPro**; V8 modifications retain their BSD notice | Actually compiled and linked; not LGPL |
| Forked glibc / IBM Accurate Mathematical Library | `third_party/glibc/README.v8` says version N/A, upstream revision `4e21c2075193e406a92c0d1cb091a7c804fda4d9`; `s_sin.c` SHA `b05c66b322da8bde56fab4ef45bdb44c8e6178494648c89f1eb64272b749d4b4` | Source headers grant **LGPL-2.1-or-later**, not LGPL-3.0-only. Present in source, excluded from this release math path |
| bubblewrap | Codex vendored 0.11.2, `vendor/bubblewrap/`; original binary SHA `77360cb751ccedc5971391444ac86a8a33c15b04d6b4a6fe45f5d25496e62c4c`; headers/COPYING LGPL-2.0-or-later | Statically linked into the separate `bwrap` wrapper executable; applicable source/relink materials supplied and tested |

Original texts: [V8 BSD license](https://raw.githubusercontent.com/v8/v8/14.9.207.2/LICENSE.v8),
[fdlibm permission](https://raw.githubusercontent.com/v8/v8/14.9.207.2/LICENSE.fdlibm),
[SPDX SunPro](https://spdx.org/licenses/SunPro.html),
[glibc source header](https://raw.githubusercontent.com/v8/v8/14.9.207.2/third_party/glibc/src/sysdeps/ieee754/dbl-64/s_sin.c),
and [bubblewrap terms](https://raw.githubusercontent.com/openai/codex/rust-v0.144.1/codex-rs/vendor/bubblewrap/COPYING).
The glibc README's broad `Shipped: yes` is upstream metadata, not this package's link evidence.
All nine glibc source/header hashes and license hashes are recorded in
[native-link-verification.json](../reports/chemcrowrun_acceptance_20261015/native-link-verification.json).
No release-number inference was substituted for the recorded upstream revision.

## Actual linking and executable verification

1. Verified the official V8 release manifest and compressed archive SHA
   `232a860a60790dc59af99e360e1f3b2ba882c8a1aaaeb055a2a482eddbce8c70`.
   Applied all three Codex V8 patches to checksum-verified V8 source in an isolated directory.
   Its Bazel source/definition files do not include the glibc math path or enable
   `V8_USE_LIBM_TRIG_FUNCTIONS`.
2. Inspected archive members and **unstripped object** symbol tables. No `s_sin`, `branred` or
   `sincostab` objects, or `glibc_sin`, `glibc_cos`, `__branred`, `__sincostab` symbols occur.
   The math object exports fdlibm `v8::base::ieee754::sin/cos`, rather than the conditional
   `fdlibm_sin/fdlibm_cos` names used when the glibc path is enabled.
3. Matched **21 entire functions in each original executable**, including sin/cos and argument
   reduction, uniquely inside executable ELF segments. Only relocation operands are masked;
   every remaining byte must match. Sin matches 914 fixed bytes out of 1102; cos matches 998
   out of 1218. This positive evidence, alongside the archive and build graph, closes the math
   applicability question. Absence of symbols in stripped executables was not used as proof.
4. Built the exact fdlibm source with GNU C++ and independently linked both the release object
   and source-built object into a math smoke harness. Both pass. GCC uses an explicit
   `__has_warning(x)=0` compatibility definition for V8's Clang warning probes. These are small
   math verification programs, **not a full V8/Codex rebuild**.
5. Bound the whole ICU data blob in **both** original programs to the locked
   `deno_core_icudata-0.77.0` crate and actual Bazel ICU commit
   `a86a32e67b8d1384b33f8fa48c83a6079b86f8cd`: 10,822,192 bytes, SHA
   `1cf67874b5a87a8363a86fb3f81e3cbbed54d389062dab8fb52308d5cf8c8612`.
   Its complete upstream license (including third-party data notices) is retained, SHA
   `451167c55c0fa447cc2d5632714f5e3c567fe4f1e1badefab2c1333852198aca`.
   The MIT wrapper declaration is not used to license ICU data.
6. Codex, code-mode-host, bwrap and rg have no `DT_NEEDED` entries; zsh dynamically needs
   libtinfo, libm and libc. This describes the ELF linkage, not a claim that static binaries
   have no third-party dependencies. Debian's dynamic glibc is separate from V8's forked math.

### Applicable relinking and replacement

The LGPL bubblewrap code is compiled into the separate `bwrap` executable, which Codex launches
across an exec boundary. Supplied materials include all four native C sources/headers, the original
Rust wrapper, libcap 2.75 source, libc crate, Cargo lock, dated modification patch, compiler pins,
archive/link commands and GNU/Linux installation instructions. The new standalone script starts
from verified companion archives and an empty Cargo/build directory; it does not use prior objects.

The modified wrapper passes version/help checks. **Unmodified original Codex then executes it via
supported system-bwrap PATH discovery**, observed by `strace`, and the sandboxed shell returns
`CHEMCROWRUN_REPLACEMENT_OK`. No bundled-digest bypass or edit is used. The replacement binary must
be outside the current project directory, as required by Codex's normal discovery logic. The original
image also starts this GNU-linked replacement through a read-only mount.

No additional Codex/code-mode-host object archive is required for the excluded V8/glibc math path.
A full rebuild of those applications was not performed and is not represented as passing. The
release pair, complete pinned application source, native math source, notices and applicable bwrap
relink materials are retained. This conclusion is specific to the listed executable hashes; a
new Codex/V8 release requires a fresh applicability check.

See [reproducible commands and material inventory](RUNTIME_LINK_VERIFICATION.md),
[clean relink/replacement receipt](../reports/chemcrowrun_acceptance_20261015/bubblewrap-clean-relink.json),
and [original-image smoke receipt](../reports/chemcrowrun_acceptance_20261015/original-image-native-smoke.json).
The original-image test uses no network, model calls or credentials and verifies Codex version,
code-mode handshake/session/JavaScript execution/shutdown, sin/cos and ICU number formatting.

## Interpretation and review boundary

- **License analysis:** component grants and applicable source/relink/notices are assessed separately;
  no blanket Apache license is assigned to the image. Actual glibc headers say 2.1-or-later.
- **Mechanical evidence:** pinned hashes, ELF sections, archive symbols, positive function matching,
  source/patch inventories and complete ICU data matches are reproducible checks, not legal advice.
- **Executed tests:** clean bwrap compile/relink, original-Codex replacement, native math harnesses
  and original-image smoke all passed. A complete Bazel rebuild and exhaustive native SBOM were
  not performed; neither is claimed by the receipts.
- **Human/professional review:** no separate legal opinion or rights-holder waiver was obtained.
  A release reviewer should confirm that all required companions remain downloadable with the
  binary, original terms/notices are retained, and no added terms restrict applicable modification,
  relinking or debugging rights. Counsel can review this evidence and any distribution-specific
  terms; no outstanding technical source/relink blocker was identified within this review's scope.

The previous review is retained in Git at `a2e8c4021`; existing 53 MB supplementary material and
historical audit/experiment payloads are unchanged. The correction does not assert that the old
review found an upstream license violation.

## Runtime limitation found during review

The bundled zsh fails to start in the original Debian 12 image because it requires `GLIBC_2.38`, while
the image has glibc 2.36. Record this as a limitation of that optional bundled executable. Codex and
the actual experiment's shell path already ran successfully. Do not silently replace zsh or upgrade
libraries inside an image still described as the original historical runtime.

## Release boundary

Staging contains `THIRD_PARTY_NOTICES.md`, supplementary source/notice material, provenance receipts,
`runtime-distribution-review.json`, the release inventory and SHA256SUMS. Prior review manifests are
preserved separately. Candidate assets are now **prepared for publication approval with required companions**. They have
not been published; the user must still approve external actions.

After publication is approved, publish the required notices and corresponding
sources alongside the binary, with explicit source-availability instructions. Retain third-party
terms and recipients' modification/relink rights. Then verify the real public URL anonymously,
including hash/import/start checks and a remote fresh clone. Local staging is not that acceptance.

The ChemCrow notebook fixture has no discovered redistribution license at its locked commit. It is
fetched from public upstream and is not a release asset. RXN source/weights are fetched from their
locked upstream sources; rebuilt RXN containers remain distinct from this historical image.
