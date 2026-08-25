from __future__ import annotations

from starlette.testclient import TestClient

from openevo_chemcrow.mcp_server import build_server


def test_rest_tool_bridge_records_pair_scoped_receipts(tmp_path, controlled_csv):
    server = build_server(
        pair_id="pair-rest",
        cache_root=tmp_path,
        controlled_chemicals_csv=controlled_csv,
        host="127.0.0.1",
        port=8765,
        network_enabled=False,
    )
    with TestClient(server.streamable_http_app()) as client:
        health = client.get("/health")
        first = client.post("/tool/SMILES2Weight", json={"query": "CCO"})
        second = client.post("/tool/SMILES2Weight", json={"query": "CCO"})
        receipts = client.get("/receipts")
    assert health.status_code == 200
    assert first.json()["observation_source"] == "live"
    assert second.json()["observation_source"] == "cache_replay"
    assert len(receipts.json()["receipts"]) == 2


def test_rest_tool_bridge_rejects_unknown_and_invalid_calls(tmp_path, controlled_csv):
    server = build_server(
        pair_id="pair-rest-invalid",
        cache_root=tmp_path,
        controlled_chemicals_csv=controlled_csv,
        host="127.0.0.1",
        port=8766,
        network_enabled=False,
    )
    with TestClient(server.streamable_http_app()) as client:
        unknown = client.post("/tool/not-a-tool", json={"query": "CCO"})
        invalid = client.post("/tool/SMILES2Weight", json={"query": ""})
        receipts = client.get("/receipts").json()["receipts"]
    assert unknown.status_code == 404
    assert invalid.status_code == 422
    assert len(receipts) == 2
    assert all(item["observation"]["error"] for item in receipts)


def test_rest_tool_bridge_records_and_caches_registry_errors(tmp_path, controlled_csv):
    server = build_server(
        pair_id="pair-rest-errors",
        cache_root=tmp_path,
        controlled_chemicals_csv=controlled_csv,
        host="127.0.0.1",
        port=8767,
        network_enabled=False,
    )
    with TestClient(server.streamable_http_app()) as client:
        first = client.post("/tool/WebSearch", json={"query": "chemistry"})
        second = client.post("/tool/WebSearch", json={"query": "chemistry"})
        receipts = client.get("/receipts").json()["receipts"]
    assert first.status_code == 200
    assert first.json()["error"].startswith("ToolUnavailableError:")
    assert first.json()["observation_source"] == "live"
    assert second.json()["observation_source"] == "cache_replay"
    assert len(receipts) == 2
