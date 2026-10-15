# ChemCrowRun service ownership

The benchmark launcher manages host Python Core Rollout, Gateway and Evolution processes with
per-workspace logs, state and PID/start-time records. This uses Core's explicitly retained legacy
v1 benchmark API. It does not bypass or claim to implement the framework-locked Desktop/Daemon
release contract. Native Reflector jobs are claimed/executed inline by the benchmark's existing
ThreeIsolatedEvolutionEngine; a generic background worker would race those claims and is not
started. The chemistry tool bridge is created per pair, never globally reused.

`gateway.nodes[].session_base_dir` binds Core's existing session/credential staging and durable
cleanup/ownership journals to this workspace. It defaults to the previous behavior when omitted;
ChemCrowRun always supplies a private `sessions/` directory, including for the dedicated Judge.
This prevents a fresh run from recovering journals belonging to an older workspace.

RXN Compose owns Redis, RabbitMQ, the Celery worker, and FastMCP/MCPO. No Redis or RabbitMQ ports
are published. Their upstream local credentials are only for this isolated Compose network. RXN
HTTP binds loopback. The managed science image is spawned by Core using the historical immutable
OCI index, host user and host networking. Docker Desktop host networking must be enabled for the
container to reach the pair-scoped bridge; Linux Docker supports this directly.

RXN is rebuilt from locked source/weights, with MCP 1.29.0 and MCPO 0.0.20 compatibility pins.
A new build is not the historical RXN image. Record actual image IDs and package versions in the
acceptance evidence; the Python base is pinned by digest and both complete dependency constraints are captured
from the 2026-10-15 functional build. The launcher stages a separate build context and installs
with these constraints without modifying the public fixture checkout. RXN worker readiness
requires a live Celery control ping. Do not label that drift as identical historical runtime conditions.

Use `chemcrowrun up`, `status`, and `down`. Core services restart only on an explicit repeat of `up`,
never by retrying a model task. Compose uses `unless-stopped`; state is scoped by project/run ID.
Stopping does not remove volumes, artifacts or logs. Model execution failures require ledger review
before explicit recovery. Do not run `docker system prune` as cleanup.
