# OpenEvo Core compatibility patch

The ChemCrowRun fork already includes this change. **Do not apply it again when
cloning the published `chemcrow` branch.**

For a separate OpenEvo checkout, the patch base is:

- Repository: https://github.com/Ardorry/OpenEvo.git
- Commit: `4bfe8f797a2026ebbcd95fb64b6ea32646c827dd`
- Patch: `openevo-session-base-dir.patch`

The patch adds `gateway.nodes[].session_base_dir` to the strict topology model
and forwards it to the existing session manager. ChemCrowRun uses this to isolate
session directories, staged credentials and cleanup/ownership journals between
experiments. It changes no scoring, model selection or evolution method.
The patch includes the two implementation lines, their documentation and the
existing workspace-isolation regression test.

```bash
git clone https://github.com/Ardorry/OpenEvo.git OpenEvo
git -C OpenEvo checkout 4bfe8f797a2026ebbcd95fb64b6ea32646c827dd
bash /path/to/ChemCrowRun/benchmarks/chemcrow/patches/apply-core-patch.sh OpenEvo
```

Then place the matching ChemCrowRun adaptation at `OpenEvo/benchmarks/chemcrow/`
and follow its REPRODUCE.md. The adaptation's `pyproject.toml` installs Core from
that checkout via `../..`; installing an arbitrary PyPI OpenEvo version is not
an equivalent substitute. The application script refuses an unexpected base or
uncommitted Core changes, and recognizes an already applied patch.

RXN needs no source patch: its locked upstream source is staged in a fresh build
directory. Retain `deployment/rxn-worker.Dockerfile`, `rxn-mcp.Dockerfile`, both
`rxn-*.constraints.txt` files and `rxn.compose.yaml`; these are the build adaptations.
