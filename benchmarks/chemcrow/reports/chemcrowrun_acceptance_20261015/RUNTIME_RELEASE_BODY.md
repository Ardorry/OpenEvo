ChemCrowRun is an independent reproduction and evaluation implementation of the ChemCrow benchmark in OpenEvo. It does not claim official ChemCrow endorsement or leaderboard certification.

This release preserves the original Linux amd64 science runtime, rather than labeling a newly built image as historical. The runtime archive is `openevo-science-runtime-0.1.1-linux-amd64.tar.gz` (352236726 bytes), SHA-256:

```text
ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149
```

OCI index identity: `sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b`.

Download and retain the required source/notice companions with the binary. `release-assets.json` enumerates exact asset sizes and hashes; verify them with `SHA256SUMS`. Start with `THIRD_PARTY_NOTICES.md`. The set includes Debian corresponding sources and notices, exact Codex source/LICENSE/NOTICE, locked dependency sources, the original supplementary source/notices, and `runtime-native-link-materials.tar.gz` with actual V8 source/release objects, ICU data/terms and independent verification scripts. These files are supplied directly alongside the binary, not as an unfulfilled written source offer.

[Distribution review](https://github.com/Ardorry/OpenEvo/blob/chemcrowrun-v1.0/benchmarks/chemcrow/docs/RUNTIME_DISTRIBUTION_REVIEW.md) distinguishes V8's BSD license, permissive fdlibm code, excluded conditional glibc math and applicable LGPL bubblewrap. [Link/replacement instructions](https://github.com/Ardorry/OpenEvo/blob/chemcrowrun-v1.0/benchmarks/chemcrow/docs/RUNTIME_LINK_VERIFICATION.md) explain how to verify the original binaries and rebuild, relink and use modified bubblewrap through supported Codex PATH discovery. Preserve original third-party license terms and recipients' applicable modification/relink/debugging rights; the image is not covered by one blanket Apache license.

Local independent verification passed: 21 fdlibm functions matched in each original Codex/code-mode-host executable, the whole ICU data blob matched, clean modified bubblewrap relink and original-Codex replacement passed, and original-image Codex/V8/ICU smoke passed without network or credentials. These are not a full V8/Codex rebuild or a professional legal opinion. Public download and remote paid reproduction have their own acceptance records and must not be inferred from local checks.

Known limitation: the optional bundled zsh needs GLIBC_2.38 and does not start in this historical Debian 12/glibc 2.36 image. It has not been silently replaced. The scientific run's working Codex/shell path is documented in the acceptance report.

Use [REPRODUCE.md](https://github.com/Ardorry/OpenEvo/blob/chemcrowrun-v1.0/REPRODUCE.md) with your own legitimate model and third-party credentials. No credentials, private run databases or model transcripts are release assets. Historical results and hashes remain intact; newer models/runtimes require explicit variant identity and fresh run state.
