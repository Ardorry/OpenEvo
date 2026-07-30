# Candidate / Evaluator Isolation Threat Model

Status: `DESIGN_FROZEN_FOR_PHASE_0`

## Security objective

The candidate must be able to solve one ResearchClawBench task using only its public workspace and one frozen OpenEvo artifact. The evaluator must later score the sealed result using hidden task material and judge credentials. No filesystem, process, environment, network, cache, or restart path may transfer evaluator-only information to the candidate or to OpenEvo's reflector.

The security boundary is the candidate namespace/runtime, not the prompt and not `cwd`.

## Assets and principals

Principals:

- **Supervisor**: trusted adapter process that owns source verification, mount construction, process lifecycle, receipts, and run publication.
- **Candidate**: untrusted autonomous agent plus every subprocess and tool it launches.
- **OpenEvo evolution worker/reflector**: receives only filtered community feedback; never receives formal-test evaluator data.
- **Evaluator**: separate post-run process that can read hidden task material and judge credentials.
- **Host**: WSL2 Linux environment plus Windows mounts and other unrelated processes/projects.

Protected assets:

- all `target_study/` trees, checklists, target papers, and target images;
- judge API key/base/model response and reasoning;
- `_score.json` and per-item score data;
- other task workspaces, trajectories, caches, and artifacts;
- ResearchClawBench repository root and evaluator source/environment;
- host home, Codex host authentication, SSH/Git/cloud credentials, process environment, Docker socket, and Windows-mounted files;
- ChemBench projects and every unrelated process/session.

## Candidate read/write policy

Candidate-readable:

- current run's `INSTRUCTIONS.md`;
- current run's `data/`;
- current run's `related_work/`;
- the exact frozen `agent_system` artifact selected by the protocol;
- runtime binaries and libraries from the pinned immutable image/rootfs.

Candidate-writable:

- current run's `code/`;
- current run's `outputs/`;
- current run's `report/` and `report/images/`;
- current task's private `/tmp` and explicitly declared private caches.

Candidate-forbidden:

- ResearchClawBench repository root and task source directory;
- every `target_study/`, checklist, target paper, or target image;
- evaluator code, process, environment, credentials, prompt, output, or cache;
- `_score.json`, judge reasoning, or per-item/hidden aggregate feedback;
- sibling run/workspace/trajectory/evolution state;
- host user directory, Windows drives, WSL interop sockets, Codex host auth, Docker socket, tmux, `/run/user`, SSH agent, Git credential helpers, and cloud metadata;
- ChemBench or any other project tree.

## Threats and controls

| Threat | Control | Evidence | Failure result |
| --- | --- | --- | --- |
| Parent/path traversal | Canonical path components, `openat`/no-follow checks, exact task allowlist | source and mount receipt | fail before launch |
| Symlink/hard-link escape | Reject links, special files, cross-root hard links, link count anomalies; recheck before/after run | file inventory plus inode identity | fail closed |
| Hidden source accidentally mounted | Explicit mount allowlist; never mount benchmark/project root | runtime mountinfo and expected/actual comparison | fail before model call |
| Environment secret inheritance | Start with empty environment and add closed allowlist | variable-name receipt; values redacted | fail before launch |
| Host process discovery | PID namespace and private `/proc`; no host `/proc` bind | namespace receipt/probe | fail before launch |
| Windows/WSL host escape | No `/mnt/*`, `/run/WSL`, WSL interop, host rootfs, or Windows path mounts | negative mount probe | fail before launch |
| Docker control escape | Never mount Docker socket or grant privileged/capabilities/device access | inspect receipt | fail before launch |
| Network retrieval of target study | Default deny; maintainer-confirmed, logged proxy allowlist only | network policy digest and egress receipt | fail or quarantine |
| Subprocess leakage | All descendants remain in the same namespaces/cgroup/process group and inherit the closed environment | cgroup/PID receipt | terminate only owned group; fail |
| Candidate/evaluator overlap | Evaluator starts only after candidate process group and namespace are gone and output is sealed | lifecycle transition receipt | no scoring |
| Cross-task memory | Fresh home, temp, cache, process namespace, and run ID per task | namespace IDs and empty-cache receipt | fail before launch |
| Score feedback loop | Official scorer deferred; feedback schema rejects official fields | feedback-filter receipt | protocol violation |

## Why `cwd` is not isolation

ResearchClawBench's current launcher sets `cwd` to the workspace but inherits `os.environ` and starts a shell. A process can still read `../`, absolute paths, `/proc`, `$HOME`, Windows mounts, repository siblings, credential files, and other processes' environment where permissions allow. It can create symlinks, spawn children, open network connections, and invoke host tools. The prompt's “stay inside this directory” statement is behavioral guidance, not an access-control rule.

Therefore `TaskRunner.run()` is unsuitable for a blind formal run. Only its workspace-construction semantics may be reused by the trusted supervisor.

## Recommended implementation: pinned Docker container plus OpenEvo credential isolation

The recommended formal implementation is one fresh Linux container per task, launched by the supervisor with a digest-pinned image and explicit mounts. This is a design only; no container is configured or launched in Phase 0.

Required properties:

1. Use an immutable image reference by digest. Do not mount the host root filesystem, repository root, `/home`, `/mnt`, Docker socket, GPU devices, or unrelated caches.
2. Use a read-only container root filesystem, `--cap-drop=ALL`, `no-new-privileges`, a non-root runtime user, a default-deny seccomp/AppArmor profile where available, a private PID/IPC/UTS namespace, and bounded PIDs/CPU/memory/storage.
3. Use distinct mounts rather than one broad workspace bind:

   - `INSTRUCTIONS.md` → `/workspace/INSTRUCTIONS.md:ro`
   - copied `data/` → `/workspace/data:ro`
   - copied `related_work/` → `/workspace/related_work:ro`
   - frozen artifact → `/openevo/session/evolution/agent_system.md:ro`
   - empty task-specific directories → `/workspace/code:rw`, `/workspace/outputs:rw`, `/workspace/report:rw`
   - private size-bounded tmpfs → `/tmp:rw,nosuid,nodev,noexec` where compatible

4. Do not mount `_meta.json`, `_score.json`, audit logs, other workspaces, source tasks, or `target_study`.
5. Start with an empty environment. Supply only the allowlist below.
6. For Codex subscription authentication, use OpenEvo's managed credential mount and nested Codex filesystem policy. The parent runtime may read the dedicated credential view, but Codex tools must be denied direct and `/proc/*/root` access; require the existing readiness receipt before task material is installed. Never mount `~/.codex` into the candidate workspace.
7. For API/proxy authentication, candidate receives only a task-scoped OpenEvo gateway token/endpoint. Provider and judge secrets stay outside the candidate container.
8. Do not use `--privileged`, host network, host PID, host user namespace, or WSL/Docker integration mounts.

Docker is recommended because it can express separate read-only and writable bind mounts cleanly. The Docker daemon is a trusted part of this design; the candidate never receives its socket. A shared daemon does not authorize stopping or inspecting unrelated containers—the supervisor records and addresses only the exact container ID it created.

### Limitation in the current OpenEvo generic runtime

OpenEvo's `BubblewrapRuntime` correctly uses `--unshare-all`, a read-only rootfs, private `/proc`, private home/tmp, cleared environment, and one session bind. However, that entire session bind is writable (`OpenEvo/src/openevo/runtime/bubblewrap.py:503`). It therefore does not by itself enforce read-only `data/` and `related_work/`. The adapter must not claim strict input immutability from that runtime alone. Either use the Docker mount profile above or add an adapter-owned nested mount profile without changing Core.

## No-Docker alternative: adapter-owned bubblewrap profile

The local host has `/usr/bin/bwrap`. The no-Docker alternative is a fresh bubblewrap namespace constructed by the adapter with an immutable, project-owned rootfs and separate file-descriptor-backed binds.

Required command semantics:

- `--die-with-parent --new-session --unshare-all`;
- do not `--share-net` unless a confirmed policy explicitly permits it;
- `--clearenv`, private `/proc`, minimal `/dev`, private tmpfs `/tmp`, private tmpfs `/home`;
- read-only bind of the rootfs and public inputs;
- separate writable binds only for `code/`, `outputs/`, and `report/`;
- no bind for the project root, host `/`, `/home`, `/mnt`, `/run`, `/sys`, or hidden task tree;
- open and pin each bind source by file descriptor, rejecting links and revalidating identity after exit;
- run under a dedicated subordinate UID where feasible. A new user namespace alone maps the caller to namespace root and is not equivalent to a different host UID; mount non-exposure remains the primary control.

If unprivileged user namespaces or bubblewrap are disabled, the run is `ISOLATION_UNAVAILABLE`; it must not fall back to host execution. An independent service account plus POSIX permissions/mount namespace is a stronger optional deployment, but it requires administrator setup and is not part of Phase 0.

## WSL2-specific recommendation

WSL2 is a Linux VM boundary, but a process inside a distro commonly sees Windows drives under `/mnt`, WSL interop sockets, and the whole distro filesystem. Neither WSL2 nor Docker Desktop integration automatically limits a candidate to the project.

Recommended WSL2 practice:

- keep runtime rootfs, task inputs, and run outputs on the Linux filesystem, not `/mnt/c`;
- do not bind Windows drives or `/run/WSL` into candidate namespaces;
- disable WSL interop inside the candidate environment by non-exposure rather than modifying global WSL settings;
- use explicit Docker/bubblewrap mounts and private `/proc`;
- do not expose Docker Desktop socket or host networking;
- record the WSL kernel and runtime-image digest in the frozen protocol.

## Environment allowlist

The supervisor constructs the environment from zero. Suggested candidate names are:

- `HOME=/home/candidate`
- `PATH=<pinned image path>`
- `LANG=C.UTF-8`
- `LC_ALL=C.UTF-8`
- `TMPDIR=/tmp`
- `PYTHONUNBUFFERED=1`
- `OPENEVO_EVOLUTION_CONTEXT`
- `OPENEVO_AGENT_SYSTEM_FILE`
- task-scoped OpenEvo proxy variables required by the selected harness

Optional names must be declared and hashed in the frozen protocol. `JUDGE_*`, host `CODEX_HOME`, `SSH_*`, `GIT_*`, cloud credentials, proxy variables, Docker variables, WSL interop variables, inherited `PYTHONPATH`, and arbitrary `*_KEY`/`*_TOKEN` are forbidden. Only variable names and value digests—not secret values—enter audit logs.

## Judge credentials

Judge credentials exist only in the evaluator process environment. The evaluator is created after candidate teardown, uses a fresh process/environment, and never shares a mount or namespace with the candidate. It receives the sealed run folder read-only and a task-specific hidden source mount read-only. `_score.json` is written outside candidate-visible paths.

Evaluator failure does not reopen the candidate. A scorer retry may repeat only the same sealed inputs and pinned scorer configuration; its attempt count and output hash are recorded.

## Network policy and target-paper retrieval risk

An unrestricted candidate can search the task wording, paper title/DOI clues, related-work citations, or figures and retrieve the target study. That defeats the blind benchmark even if `target_study/` is not mounted.

The conservative policy is:

- workspace smoke and canary construction: network disabled;
- community Dev: network disabled by default; any allowlisted proxy must be frozen before scores are seen;
- community Validation: same frozen policy, with no candidate-specific changes;
- official 40: `PENDING_MAINTAINER_CONFIRMATION`; until confirmed, network disabled and the run is not submitted as an official score if this departs from the official protocol.

If network is authorized, use a task-independent egress proxy with domain/method/byte/time limits, no private-network or cloud-metadata routes, DNS logging, no browser/CDP control, and a predeclared package mirror. Do not place a hidden-paper denylist in the prompt or candidate filesystem. Network logs prove destinations, not semantic non-retrieval; uncertainty is disclosed.

## Path-escape and link policy

Before mount and after candidate exit:

- resolve every expected component relative to an opened root descriptor;
- reject absolute, empty, `.`, `..`, NUL, control-character, overlong, and Unicode-normalization-ambiguous paths;
- reject symlinks, junction-like paths, FIFOs, sockets, devices, and unexpected hard links;
- reject report links and Markdown/image references resolving outside the run root;
- do not follow links while inventorying or packaging;
- compare path identity with held file-descriptor identity before launch, before evaluator start, and before publication.

A candidate-created unsafe entry makes artifact validation fail; it is preserved as evidence but is never opened by the evaluator or packager.

## Subprocess and process lifecycle

Candidate subprocesses are allowed only because scientific analysis needs tools. They remain in the same PID/mount/network/user namespace, cgroup, resource limits, environment policy, and process group. The supervisor sets itself as lifecycle owner and tracks the exact root PID/container ID.

Lifecycle states are closed:

```text
ALLOCATED -> CONSTRUCTED -> ISOLATION_VERIFIED -> CANDIDATE_RUNNING
-> CANDIDATE_TERMINAL -> NAMESPACE_GONE -> OUTPUT_SEALED
-> VALIDATED -> EVALUATOR_RUNNING -> SCORED -> PACKAGED
```

No evaluator state may coexist with `CANDIDATE_RUNNING`. Timeout/cancel logic signals only the exact process group/container created for this run; it never uses process-name matching.

## Per-task state and cache namespaces

Each run gets unique:

- home directory;
- `/tmp`;
- package/cache directories;
- OpenEvo session and transcript directory;
- candidate process and network namespace;
- code/output/report directories;
- evaluator process and cache;
- run/evaluation receipt IDs.

No candidate cache is reused across formal tasks. Dependencies are prebuilt into the pinned image or placed in a read-only task-independent package cache whose digest is frozen. Native Codex memory is cleared/disabled for formal independence unless the maintainer explicitly approves a different fixed policy.

## Evidence that hidden content was unavailable

Absolute proof of non-access is impossible from application logs alone. The protocol establishes strong structural assurance through non-exposure:

1. expected mount specification and digest;
2. runtime inspect or `/proc/self/mountinfo` captured from a trusted probe before candidate launch;
3. negative probes for benchmark root, `target_study`, host home, `/mnt`, Docker socket, sibling workspaces, and credential paths;
4. private PID namespace evidence and visible PID inventory;
5. environment-name inventory and secret-shape scan;
6. source and destination inode/device/mode identities;
7. network namespace/policy digest and bounded egress log;
8. start/end input hashes and absence of unsafe links;
9. candidate/evaluator non-overlap lifecycle receipt;
10. feedback-filter receipt proving no official score release.

Hidden-directory hashes alone do not prove non-access and must not be presented as such.

## Audit logs

Retain, per run:

- protocol, source, request, prompt, artifact, image/rootfs, mount, and network digests;
- exact candidate container/namespace/run IDs;
- environment variable names and redacted value digests;
- runtime inspect/mount evidence and negative-probe results;
- stdout/stderr/exit/timeout metadata;
- file inventory and validator receipt;
- evaluator start only after namespace teardown;
- feedback release decision and rejection codes;
- all retry lineage.

Logs must never contain judge secrets, checklist text/keywords, target-study content, provider credentials, host auth, or unredacted environment dumps.

## Fail-closed conditions

Do not launch or score when any of the following occurs:

- source commit/revision/split/artifact/image/policy digest mismatch;
- unexpected tracked source modification;
- isolation primitive or private namespace unavailable;
- mount list differs from the closed allowlist;
- candidate can see a forbidden negative-probe path;
- read-only input is writable;
- host environment/credential inheritance is detected;
- candidate and evaluator lifetimes overlap;
- unsafe link/path/special file is present;
- candidate remains alive after teardown;
- output or transcript exceeds a bound without an explicit truncation receipt;
- artifact validator fails;
- scorer is invoked in a prohibited phase;
- feedback record fails the closed schema;
- official network, Pass@5, or cross-task policy remains unconfirmed for the requested mode.

There is no fallback to `cwd`-only execution.
