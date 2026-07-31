# Frozen project fork authority

`POST /v2/internal/frozen-project-forks` is an authenticated Core-control
primitive for seeding an independent project from one immutable
`ProjectFreezeAuthorityV1`. It is intended for frozen evaluation runs that need
fresh project/workspace/session ownership while retaining an exact, audited
artifact composition.

The destination must be a ready, unused generation-zero project. Core preserves
its workspace and execution snapshots, creates destination-owned evolution and
runtime-context references, and inherits exactly the frozen `agent_system`,
`skill_bundle`, and `text_memory` registry authorities. The resulting adjacent
head and `AtomicFrozenProjectForkManifestV1` are committed in the same SQLite
transaction. The source project and freeze receipt are never mutated.

The operation is fail-closed on source freeze/head drift, destination reuse,
configuration drift, missing native successor authority, incomplete artifact
closure, or idempotency-key conflict. Replaying the exact request returns the
same authority. A different request cannot seed the same destination or reuse
the same operation identity. The receipt remains readable after Core restart,
and runtime binding treats it as a materialized inherited context.

This endpoint is installed only on the internal Core v2 app, so the normal
generation-bound Core-control bearer protection applies. Callers receive only
content-addressed IDs and hashes; no runtime credential is part of the request
or receipt.
