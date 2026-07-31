# Formal v11 control surface

`formal-v11-prepare` is append-only. In addition to new Community and official
run IDs, it requires explicit paths for a newly built framework lock, daemon
bundle and manifest, managed-reflector readiness receipt, and generation-bound
reflector credential-mount readiness receipt. The preparer verifies file
ownership, hashes, bundle size, Core wheel/registry identity, release identity,
Codex 0.144.1 runtime identity, and mount-receipt generation/release bindings.
It rejects any selected path inherited from the source protocol.

Formal v11 mutations must use `formal-community-*`; the legacy
`training-start`, `run-next`, and `resume` command surface fails closed for a
formal protocol. Before `formal-community-init` or `formal-community-start`
creates a namespace, an operation-scoped managed Core attachment runs no-model
candidate capability, generation/release, reflector mount, Judge readiness,
feedback transport, successor, registry and freeze checks. No candidate or
evolution intent is persisted by this preflight.

The official block carries its own closed budget. Terminal Community and
official states stop `--until` loops immediately, preventing an unreachable
target stage from causing repeated transitions or side effects.
