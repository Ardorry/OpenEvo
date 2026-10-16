# Original runtime downloads

The delivery uses two large attachments outside Git:

| Attachment | Purpose |
| --- | --- |
| [openevo-science-runtime-0.1.1-linux-amd64.tar.gz](https://github.com/Ardorry/OpenEvo/releases/download/chemcrowrun-runtime-0.1.1/openevo-science-runtime-0.1.1-linux-amd64.tar.gz) | Original Linux amd64 OCI archive, 352,236,726 bytes |
| [chemcrowrun-runtime-sources-and-licenses.tar.gz](https://github.com/Ardorry/OpenEvo/releases/download/chemcrowrun-runtime-0.1.1/chemcrowrun-runtime-sources-and-licenses.tar.gz) | Corresponding sources, licenses, copyright notices, patches and relink/build materials |

The asset container is the [runtime delivery release](https://github.com/Ardorry/OpenEvo/releases/tag/chemcrowrun-runtime-0.1.1),
not a claim that ChemCrowRun stable v1.0 or public paid end-to-end acceptance has passed.
Its tag points to the source revision used for this delivery. The evolving `chemcrow`
branch can receive later documentation or adaptation fixes without changing that tag.

Install the runtime with the `chemcrowrun runtime --url ...` command in
[REPRODUCE.md](../REPRODUCE.md). Docker must be available. This imports the original
image; no host Codex executable or image rebuild is required. Core launches Codex
inside the managed image. The image contains Codex 0.144.1 and retains the original
runtime identity. Its optional zsh requires GLIBC_2.38, while the image's Debian
environment supplies GLIBC_2.36; use the documented normal execution path.

The companion is an additive package of the previously prepared source/notice archives.
Extract it into an empty directory and read its README.md and THIRD_PARTY_NOTICES.md.
Its original source archives retain their bytes, notices and modification records.
There are no separate audit or checksum-report attachments. Normal runtime integrity
checks still use the pinned identity in the source code; member provenance remains
with the source/license material. See [runtime-assets.json](runtime-assets.json) for
the two download identities.

The paired results and 42 published artifacts are from the prior real local run.
Public remote fresh-clone paid end-to-end acceptance is still pending.
