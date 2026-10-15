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
