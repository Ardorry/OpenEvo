# ChemCrowRun services

`chemcrowrun up` starts the OpenEvo Core Rollout, Gateway and Evolution services for one
experiment workspace. Logs, state, sessions and generated artifacts remain under that workspace.
The optional RXN Compose stack provides Redis, RabbitMQ, a Celery prediction worker and an
MCP-compatible HTTP service. Redis and RabbitMQ are internal to the Compose network; RXN HTTP
binds to loopback.

ChemCrowRun uses `gateway.nodes[].session_base_dir` to keep Core sessions in the active run
workspace. The benchmark executes its three Reflector methods through OpenEvo Core and provides
the resulting text memory, skill bundle and agent-system artifacts to G2.

The managed science runtime is loaded separately as described in
[`../reproduction/RUNTIME_DOWNLOADS.md`](../reproduction/RUNTIME_DOWNLOADS.md). The RXN service
images are built from the public RXN fixture with the dependency constraints in this directory.

Use these commands from the repository root:

~~~bash
uv run --frozen --project benchmarks/chemcrow chemcrowrun up --workspace <workspace>
uv run --frozen --project benchmarks/chemcrow chemcrowrun status --workspace <workspace>
uv run --frozen --project benchmarks/chemcrow chemcrowrun logs --workspace <workspace>
uv run --frozen --project benchmarks/chemcrow chemcrowrun down --workspace <workspace>
~~~
