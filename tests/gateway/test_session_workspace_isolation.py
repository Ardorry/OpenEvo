from pathlib import Path

from openevo.config import TopologyConfig
from openevo.gateway.server import _build_state


def test_same_node_name_in_distinct_workspaces_has_distinct_recovery_roots(tmp_path: Path):
    states = []
    for name in ("clone-a", "clone-b"):
        root = tmp_path / name
        root.mkdir()
        topology = TopologyConfig.model_validate(
            {
                "gateway": {
                    "completion_persistence": {"enabled": False},
                    "nodes": [
                        {
                            "id": "same-node",
                            "host": "127.0.0.1",
                            "port": 18081,
                            "session_base_dir": str(root),
                        }
                    ],
                },
            }
        )
        states.append(_build_state(topology, "same-node"))
    first, second = (state.node_manager for state in states)
    assert first._session_base_dir != second._session_base_dir
    assert first._cleanup_journal_dir != second._cleanup_journal_dir
    assert first._docker_ownership_root != second._docker_ownership_root
    assert first._cleanup_journal_dir.is_relative_to(tmp_path / "clone-a")
    for state in states:
        state.storage.close()
