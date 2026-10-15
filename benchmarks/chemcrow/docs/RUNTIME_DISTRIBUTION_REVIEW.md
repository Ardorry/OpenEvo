# Original runtime distribution review

This review concerns the original historical OCI archive, not a reconstructed image. Its verified
SHA-256 is `ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149`.
The image is unchanged. No release asset or public image has been uploaded.

## Evidence prepared

| Component | Evidence available | Remaining scope |
| --- | --- | --- |
| OCI filesystem | All ten layers scanned, including deleted entries; 17,480 members | Bounded text/known-pattern scanning is not a proof that every possible secret is absent |
| Debian packages | 180 installed binary/source inventory entries; all 104 exact source versions obtained; copyright notices and corresponding-source companion archive | Confirm that approved release assets keep the notices and corresponding sources available alongside the binary archive |
| Python, pip, Node, npm, corepack | Installed version inventory and bundled license/notice files copied to a companion archive | Applicability of bundled components is reviewed separately from their runtime identity |
| Codex 0.144.1 | Matching upstream LICENSE, NOTICE, source tag and Cargo.lock | The upstream project license does not alone describe every native dependency |
| Rust registry dependencies | All 1193 Cargo.lock registry archives downloaded and checksum verified; license files and upstream supplements collected | The workspace lock includes development and other-platform dependencies, so this is an overinclusive collection |
| Git dependencies | Seven repositories obtained at exact locked revisions, with notices | Same distinction between available source and actual binary dependency applicability |
| Native embedded code / compiler runtime | Source and notice candidates collected where present in the preceding archives | Consolidated applicability review remains open; no exact linked-dependency inventory is claimed |

The [machine-readable notice review](../reports/chemcrowrun_acceptance_20261015/runtime-notice-review.json)
records 43 packages initially flagged because no standalone license filename was found. Several have
copyright or permission text in source headers; others have notices in their exact upstream source
tree. A missing standalone filename does **not** prove a missing license grant or a licensing
violation. SPDX declarations, complete source archives and retained headers are relevant evidence.
Conversely, finding a notice filename does not by itself establish its applicability to a binary.

The upstream Codex distribution uses [Apache-2.0](https://raw.githubusercontent.com/openai/codex/rust-v0.144.1/LICENSE)
and includes a [NOTICE](https://raw.githubusercontent.com/openai/codex/rust-v0.144.1/NOTICE) identifying
derived Ratatui code. Both exact files are present in staging. Their retention is part of the release
preparation; it is not a blanket clearance for every bundled component.

## Release boundary

`runtime-manifest.json` keeps redistribution review separate from publication permission. The
archive/import/start checks have passed. Redistribution review is still `PENDING_LICENSE_OBLIGATION_REVIEW`;
publication is `PENDING_USER_APPROVAL`. Neither status may be silently promoted by a checksum check.

The release staging inventory lists the original archive, exact source companions, notices,
software inventories, scans and SHA256SUMS. Additional review evidence is preserved rather than
replacing original historical data. Do not upload temporary downloads, private logs, credentials,
experiment sessions or unresolved partial downloads.

The ChemCrow notebook fixture has no discovered redistribution license at its locked commit.
The reproduction tool fetches it from its public upstream; the fixture is not a release asset.
RXN source and weights are also fetched from their locked upstream sources. Rebuilt RXN containers
are a separately recorded environment and are not represented as the original science-runtime image.

After approved publication, anonymous download and hash/import/start verification must be run using
the actual asset URL. A local staging copy, checksum pass or local clone does not establish that gate.
