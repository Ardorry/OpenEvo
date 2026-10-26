# ChemCrowRun v1.0 release preparation

This candidate retains the original machine protocol and all historical payload hashes. New public
names are aliases, not renamed historical runs. Existing Python imports and `openevo-chemcrow`
commands remain compatible. No official ChemCrow endorsement or leaderboard acceptance is claimed.

Candidate assets:

- Source commit and local acceptance report (branch `codex/chemcrowrun-v1`).
- Original `openevo-science-runtime-0.1.1-linux-amd64.tar.gz`, SHA-256
  `ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149`, 352236726 bytes.
- SHA256SUMS, OCI identity manifest, all-layer secret scan, software inventory and third-party notices.
- Sanitized new-run report/submission only after genuine execution and PASS audit.

The original runtime has passed local archive/import/start checks. The bounded text scan inspects
all layers, including removed files, for known credential paths and high-confidence secret patterns;
this is evidence with a stated scope, not a proof of absence of every possible secret. Redistribution
requires preserving bundled licenses and satisfying source/notice requirements of included packages.
The Codex package declares Apache-2.0 but its npm installation has no LICENSE file: attach the matching
license and required notices separately before public asset distribution. Debian copyright files and
exact binary/source package inventory must be included in the review. Do not publish an asset while
its redistribution clearance remains PENDING.

The original ChemCrow notebook repository has no discovered license and is fetched from upstream,
not redistributed. ChemCrow source and rxn4chemistry declare MIT; rxn-sandbox uses OpenMDW-1.1 and
its license/notice conditions apply to redistributed code/weights. The candidate fetches them directly
instead of including a private copy in the runtime archive.

Publication is a separate, user-approved step: push the reviewed branch, create a formal PR, publish
approved release assets (or approved public images), then anonymously download and verify the exact
asset and repeat fresh-clone acceptance from the actual remote commit. A local clone or cached
archive does not certify public delivery. No force push, historical rewrite, or runtime substitution.

Local review evidence on 2026-10-15: ten OCI layers, 17,480 members, 352 filename-matched notice/license candidates;
no matches in the stated bounded scan. All 104 exact Debian source package versions were
downloaded and a corresponding-source companion archive prepared. Matching Codex 0.144.1
LICENSE and NOTICE were obtained from its upstream tag. The follow-up actual-link audit closes the V8/glibc applicability gap and supplies exact native/data
notices. Redistribution is evidence-supported with the complete source/notice companions; the
automated scan alone does not grant legal clearance. RXN source/weights are fetched, not included in the original runtime.

Matching source and checksum-verified archives for all 1,193 registry dependencies in Codex's
workspace Cargo.lock and seven exact Git dependency repositories have also been collected. This
overinclusive inventory is not an exact native binary link SBOM. Source availability and filename
matches alone do not establish complete attribution coverage. Do not conflate this review with the
separately passed historical image identity checks.

See the [dated engineering acceptance record](../reports/chemcrowrun_acceptance_20261015/ACCEPTANCE.md)
for actual execution, model substitution, costs, service isolation and remaining publication gates.
The [runtime distribution review](RUNTIME_DISTRIBUTION_REVIEW.md) separates missing standalone
filenames from actual attribution questions and lists the evidence prepared for each component.

The original 53 MB supplement remains unchanged. The additive native-link companion records the
actual Bazel V8 source/static archive, full ICU data binding, clean LGPL bwrap relink and replacement,
and read-only original-image smoke. See [repeatable verification](RUNTIME_LINK_VERIFICATION.md).
External publication still requires user approval; public delivery remains untested.
