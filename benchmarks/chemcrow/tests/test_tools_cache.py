from __future__ import annotations

import pytest

from openevo_chemcrow.cache import PairObservationCache, assert_real_metric_observations
from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.models import ObservationSource, ToolObservation
from openevo_chemcrow.tools import ChemCrowToolRegistry, ToolUnavailableError


def test_local_tools_are_wrapped(controlled_csv):
    registry = ChemCrowToolRegistry(
        controlled_chemicals_csv=controlled_csv,
        network_enabled=False,
    )
    weight = registry.execute("SMILES2Weight", {"query": "CCO"}, call_id="w")
    similarity = registry.execute("MolSimilarity", {"query": "CCO.CCOC"}, call_id="s")
    groups = registry.execute("FunctionalGroups", {"query": "CCO"}, call_id="g")
    assert weight.error is None and weight.result["exact_molecular_weight"] > 40
    assert 0 <= similarity.result["tanimoto"] <= 1
    assert "functional_groups" in groups.result


def test_missing_api_key_and_excluded_tool_fail_explicitly(monkeypatch):
    monkeypatch.delenv("SERP_API_KEY", raising=False)
    registry = ChemCrowToolRegistry(network_enabled=True)
    with pytest.raises(ToolUnavailableError, match="SERP_API_KEY"):
        registry.execute("WebSearch", {"query": "chemistry"}, call_id="web")
    with pytest.raises(ToolUnavailableError, match="excluded"):
        registry.execute("python_repl", {"query": "2+2"}, call_id="python")


def test_mock_requires_explicit_fixture_and_cannot_enter_real_metrics(tmp_path):
    arguments = {"query": "CCO"}
    key = canonical_sha256({"tool_name": "SMILES2Weight", "arguments": arguments})
    registry = ChemCrowToolRegistry(mode="mock", fixtures={key: {"value": 1}})
    observation = registry.execute("SMILES2Weight", arguments, call_id="mock")
    assert observation.source == ObservationSource.MOCK
    with pytest.raises(ValueError, match="fixture/mock"):
        assert_real_metric_observations([observation])
    missing = ChemCrowToolRegistry(mode="mock")
    with pytest.raises(ToolUnavailableError, match="explicit matching fixture"):
        missing.execute("SMILES2Weight", arguments, call_id="missing")


def test_pair_cache_replays_only_inside_pair(tmp_path):
    calls = 0
    arguments = {"query": "CCO"}

    def live():
        nonlocal calls
        calls += 1
        return ToolObservation(
            call_id="live",
            tool_name="SMILES2Weight",
            canonical_arguments_sha256=canonical_sha256(arguments),
            result={"value": 46},
            source=ObservationSource.LIVE,
        )

    pair_a = PairObservationCache(tmp_path, pair_id="pair-a")
    first = pair_a.execute(call_id="a1", tool_name="SMILES2Weight", arguments=arguments, live=live)
    replay = pair_a.execute(call_id="a2", tool_name="SMILES2Weight", arguments=arguments, live=live)
    pair_b = PairObservationCache(tmp_path, pair_id="pair-b")
    second_pair = pair_b.execute(call_id="b1", tool_name="SMILES2Weight", arguments=arguments, live=live)
    assert first.source == ObservationSource.LIVE
    assert replay.source == ObservationSource.CACHE_REPLAY
    assert second_pair.source == ObservationSource.LIVE
    assert calls == 2


def test_local_rxn_uses_official_mcpo_payload_and_fails_closed(monkeypatch):
    requests = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(url, *, json, timeout, trust_env):
        requests.append((url, json, timeout, trust_env))
        return Response({"status": "success", "result": []})

    monkeypatch.setattr("openevo_chemcrow.tools.httpx.post", fake_post)
    monkeypatch.setenv("CHEMCROW_RXN_PREDICT_URL", "http://rxn/product_prediction")
    monkeypatch.setenv("CHEMCROW_RXN_RETRO_URL", "http://rxn/retro_prediction")
    registry = ChemCrowToolRegistry(network_enabled=True)
    prediction = registry.execute("ReactionPredict", {"query": "CCO.O"}, call_id="forward")
    retro = registry.execute("ReactionRetrosynthesis", {"query": "CCO"}, call_id="retro")
    assert prediction.error is None and retro.error is None
    assert requests[0][1]["reactants_list"] == ["CCO.O"]
    assert requests[1][1]["product"] == "CCO"
    assert requests[0][1]["device"] == requests[1][1]["device"] == "cpu"
    assert requests[0][3] is False and requests[1][3] is False

    monkeypatch.setattr(
        "openevo_chemcrow.tools.httpx.post",
        lambda *args, **kwargs: Response({"status": "error", "message": "worker unavailable"}),
    )
    failed = registry.execute("ReactionPredict", {"query": "CCO"}, call_id="failed")
    assert "worker unavailable" in (failed.error or "")
