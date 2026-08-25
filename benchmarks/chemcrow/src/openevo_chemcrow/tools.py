from __future__ import annotations

import csv
import importlib.util
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .hashing import canonical_sha256
from .models import ObservationSource, ToolObservation


class ToolUnavailableError(RuntimeError):
    pass


class ToolInputError(ValueError):
    pass


TOOL_INVENTORY: tuple[dict[str, Any], ...] = (
    {
        "name": "python_repl",
        "purpose": "arbitrary Python execution",
        "import_path": "langchain.agents.load_tools:python_repl",
        "class": "F",
        "local_network": "local",
        "determinism": "non-deterministic",
        "dependencies": ["legacy langchain"],
        "service": None,
        "environment_variables": [],
        "paper_only_absent": False,
        "safe": False,
        "human_setup": True,
        "status": "excluded",
        "failure_reason": "arbitrary code execution is excluded from the benchmark tool surface",
    },
    {
        "name": "wikipedia",
        "purpose": "general encyclopedia lookup",
        "import_path": "langchain.agents.load_tools:wikipedia",
        "class": "B",
        "local_network": "public network",
        "determinism": "non-deterministic",
        "dependencies": ["httpx"],
        "service": "MediaWiki API",
        "environment_variables": [],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": False,
        "status": "available when network is enabled",
        "failure_reason": None,
    },
    {
        "name": "Name2SMILES",
        "purpose": "convert a compound name to canonical SMILES",
        "import_path": "chemcrow.tools.converters.Query2SMILES",
        "class": "B",
        "local_network": "public network",
        "determinism": "non-deterministic",
        "dependencies": ["rdkit", "httpx"],
        "service": "PubChem PUG REST; optional ChemSpace fallback",
        "environment_variables": ["CHEMSPACE_API_KEY"],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": False,
        "status": "available when PubChem is reachable",
        "failure_reason": None,
    },
    {
        "name": "Mol2CAS",
        "purpose": "convert molecule name or SMILES to CAS registry number",
        "import_path": "chemcrow.tools.converters.Query2CAS",
        "class": "B",
        "local_network": "public network",
        "determinism": "non-deterministic",
        "dependencies": ["rdkit", "httpx"],
        "service": "PubChem PUG REST/PUG View",
        "environment_variables": [],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": False,
        "status": "available when PubChem is reachable",
        "failure_reason": None,
    },
    {
        "name": "SMILES2Name",
        "purpose": "convert SMILES to a compound name",
        "import_path": "chemcrow.tools.converters.SMILES2Name",
        "class": "B",
        "local_network": "public network",
        "determinism": "non-deterministic",
        "dependencies": ["rdkit", "httpx"],
        "service": "PubChem PUG REST",
        "environment_variables": [],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": False,
        "status": "available when PubChem is reachable",
        "failure_reason": None,
    },
    {
        "name": "PatentCheck",
        "purpose": "SureChEMBL membership lookup",
        "import_path": "chemcrow.tools.search.PatentCheck",
        "class": "A",
        "local_network": "local",
        "determinism": "deterministic for pinned Bloom data",
        "dependencies": ["molbloom"],
        "service": None,
        "environment_variables": [],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": False,
        "status": "available after locked environment install",
        "failure_reason": None,
    },
    {
        "name": "MolSimilarity",
        "purpose": "Morgan-fingerprint Tanimoto similarity",
        "import_path": "chemcrow.tools.rdkit.MolSimilarity",
        "class": "A",
        "local_network": "local",
        "determinism": "deterministic",
        "dependencies": ["rdkit"],
        "service": None,
        "environment_variables": [],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": False,
        "status": "available after locked environment install",
        "failure_reason": None,
    },
    {
        "name": "SMILES2Weight",
        "purpose": "exact molecular weight",
        "import_path": "chemcrow.tools.rdkit.SMILES2Weight",
        "class": "A",
        "local_network": "local",
        "determinism": "deterministic",
        "dependencies": ["rdkit"],
        "service": None,
        "environment_variables": [],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": False,
        "status": "available after locked environment install",
        "failure_reason": None,
    },
    {
        "name": "FunctionalGroups",
        "purpose": "SMARTS functional-group detection",
        "import_path": "chemcrow.tools.rdkit.FuncGroups",
        "class": "A",
        "local_network": "local",
        "determinism": "deterministic",
        "dependencies": ["rdkit"],
        "service": None,
        "environment_variables": [],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": False,
        "status": "available after locked environment install",
        "failure_reason": None,
    },
    {
        "name": "ExplosiveCheck",
        "purpose": "check PubChem GHS explosive classification",
        "import_path": "chemcrow.tools.safety.ExplosiveCheck",
        "class": "B",
        "local_network": "public network",
        "determinism": "non-deterministic",
        "dependencies": ["httpx"],
        "service": "PubChem PUG View",
        "environment_variables": [],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": False,
        "status": "available through lazy adapter; vendor constructor's ClinTox download avoided",
        "failure_reason": None,
    },
    {
        "name": "ControlChemCheck",
        "purpose": "controlled-chemical membership and similarity safety gate",
        "import_path": "chemcrow.tools.safety.ControlChemCheck",
        "class": "A",
        "local_network": "local for SMILES; PubChem for CAS/name",
        "determinism": "deterministic locally",
        "dependencies": ["rdkit", "chem_wep_smi.csv"],
        "service": "optional PubChem conversion",
        "environment_variables": [],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": False,
        "status": "available after locked environment install and vendor data binding",
        "failure_reason": None,
    },
    {
        "name": "SimilarityToControlChem",
        "purpose": "similarity to the controlled-chemical list",
        "import_path": "chemcrow.tools.safety.SimilarControlChemCheck",
        "class": "A",
        "local_network": "local",
        "determinism": "deterministic",
        "dependencies": ["rdkit", "chem_wep_smi.csv"],
        "service": None,
        "environment_variables": [],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": False,
        "status": "available after locked environment install and vendor data binding",
        "failure_reason": None,
    },
    {
        "name": "SafetySummary",
        "purpose": "summarize hazard and safety records",
        "import_path": "chemcrow.tools.safety.SafetySummary",
        "class": "B",
        "local_network": "public network",
        "determinism": "non-deterministic",
        "dependencies": ["httpx"],
        "service": "PubChem PUG View",
        "environment_variables": [],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": False,
        "status": "available as deterministic evidence extract without legacy hidden LLM",
        "failure_reason": None,
    },
    {
        "name": "LiteratureSearch",
        "purpose": "search papers and synthesize an answer",
        "import_path": "chemcrow.tools.search.Scholar2ResultLLM",
        "class": "F",
        "local_network": "public network plus model",
        "determinism": "non-deterministic",
        "dependencies": ["paper-qa==1.1.1", "paperscraper", "legacy langchain"],
        "service": "Semantic Scholar and legacy OpenAI embeddings/LLM",
        "environment_variables": ["OPENAI_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": True,
        "status": "excluded from initial real profile",
        "failure_reason": "would add hidden legacy model calls and break paired provider fairness",
    },
    {
        "name": "WebSearch",
        "purpose": "general web search",
        "import_path": "chemcrow.tools.search.WebSearch",
        "class": "C",
        "local_network": "credentialed network",
        "determinism": "non-deterministic",
        "dependencies": ["httpx"],
        "service": "SerpAPI",
        "environment_variables": ["SERP_API_KEY"],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": True,
        "status": "blocked without key",
        "failure_reason": "SERP_API_KEY is not configured",
    },
    {
        "name": "GetMoleculePrice",
        "purpose": "query compound availability and price",
        "import_path": "chemcrow.tools.chemspace.GetMoleculePrice",
        "class": "F",
        "local_network": "credentialed commercial network",
        "determinism": "non-deterministic",
        "dependencies": ["httpx"],
        "service": "ChemSpace API",
        "environment_variables": ["CHEMSPACE_API_KEY"],
        "paper_only_absent": False,
        "safe": False,
        "human_setup": True,
        "status": "excluded from benchmark execution profile",
        "failure_reason": "procurement/price tool is unnecessary and excluded by benchmark-only boundary",
    },
    {
        "name": "ReactionPredict",
        "purpose": "forward reaction prediction",
        "import_path": "chemcrow.tools.rxn4chem.RXNPredict or chemcrow.tools.reactions.RXNPredictLocal",
        "class": "C/D",
        "local_network": "hosted API or local service",
        "determinism": "model-dependent",
        "dependencies": ["rxn4chemistry for hosted path", "HTTP for local path"],
        "service": "IBM RXN or RXN-Sandbox/local endpoint",
        "environment_variables": ["RXN4CHEM_API_KEY", "RXN4CHEM_PROJECT_ID", "CHEMCROW_RXN_PREDICT_URL"],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": True,
        "status": "local RXN-Sandbox service passed endpoint and forward-prediction smoke",
        "failure_reason": None,
    },
    {
        "name": "ReactionRetrosynthesis",
        "purpose": "retrosynthetic route prediction",
        "import_path": "chemcrow.tools.rxn4chem.RXNRetrosynthesis or chemcrow.tools.reactions.RXNRetrosynthesisLocal",
        "class": "C/D",
        "local_network": "hosted API or local service",
        "determinism": "model-dependent",
        "dependencies": ["rxn4chemistry for hosted path", "HTTP for local path"],
        "service": "IBM RXN or RXN-Sandbox/local endpoint",
        "environment_variables": ["RXN4CHEM_API_KEY", "RXN4CHEM_PROJECT_ID", "CHEMCROW_RXN_RETRO_URL"],
        "paper_only_absent": False,
        "safe": True,
        "human_setup": True,
        "status": "local RXN-Sandbox service passed endpoint and single-step retro smoke",
        "failure_reason": None,
    },
    {
        "name": "paper-only restricted tools",
        "purpose": "tools described in the ChemCrow paper but omitted from public source",
        "import_path": "absent from chemcrow-public",
        "class": "E",
        "local_network": "unknown/restricted",
        "determinism": "unknown",
        "dependencies": [],
        "service": "restricted APIs",
        "environment_variables": [],
        "paper_only_absent": True,
        "safe": False,
        "human_setup": True,
        "status": "unavailable",
        "failure_reason": "the official README states that API-restricted paper tools are not public",
    },
)


_FUNCTIONAL_GROUPS = {
    "aldehydes": "[CX3H1](=O)[#6]",
    "esters": "[#6][CX3](=O)[OX2H0][#6]",
    "ketones": "[#6][CX3](=O)[#6]",
    "amides": "C(=O)-N",
    "thiols": "[SH]",
    "alcohols": "[OH]",
    "carboxylic_acids": "C(=O)[O;D1]",
    "nitro": "[N;D3](=[O;D1])[O;D1]",
    "cyano": "[C;D2]#[N;D1]",
    "halogens": "[#9,#17,#35,#53]",
    "primary_amines": "[N;D1]",
}


def _rdkit() -> tuple[Any, Any, Any]:
    try:
        from rdkit import Chem, DataStructs
        from rdkit.Chem import AllChem, rdMolDescriptors
    except ImportError as exc:
        raise ToolUnavailableError("rdkit is not installed in the locked tool environment") from exc
    return Chem, DataStructs, (AllChem, rdMolDescriptors)


def _molecule(smiles: str) -> Any:
    Chem, _, _ = _rdkit()
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise ToolInputError("invalid SMILES string")
    return molecule


class ChemCrowToolRegistry:
    def __init__(
        self,
        *,
        controlled_chemicals_csv: Path | None = None,
        network_enabled: bool = True,
        timeout_seconds: float = 20.0,
        mode: str = "real",
        fixtures: dict[str, Any] | None = None,
    ) -> None:
        if mode not in {"real", "mock"}:
            raise ValueError("tool mode must be real or mock")
        self.controlled_chemicals_csv = controlled_chemicals_csv
        self.network_enabled = network_enabled
        self.timeout_seconds = timeout_seconds
        self.mode = mode
        self.fixtures = fixtures or {}

    def execute(self, tool_name: str, arguments: dict[str, Any], *, call_id: str) -> ToolObservation:
        started = time.monotonic()
        if self.mode == "mock":
            fixture_key = canonical_sha256({"tool_name": tool_name, "arguments": arguments})
            if fixture_key not in self.fixtures:
                raise ToolUnavailableError("MOCK mode requires an explicit matching fixture")
            return ToolObservation(
                call_id=call_id,
                tool_name=tool_name,
                canonical_arguments_sha256=canonical_sha256(arguments),
                result=self.fixtures[fixture_key],
                source=ObservationSource.MOCK,
                elapsed_seconds=time.monotonic() - started,
            )
        try:
            result = self._execute_real(tool_name, arguments)
            error = None
        except (ToolUnavailableError, ToolInputError):
            raise
        except (httpx.HTTPError, ImportError, KeyError, RuntimeError, TypeError, ValueError) as exc:
            result = None
            error = f"{type(exc).__name__}: {exc}"
        return ToolObservation(
            call_id=call_id,
            tool_name=tool_name,
            canonical_arguments_sha256=canonical_sha256(arguments),
            result=result,
            error=error,
            source=ObservationSource.LIVE,
            elapsed_seconds=time.monotonic() - started,
        )

    @staticmethod
    def _query(arguments: dict[str, Any]) -> str:
        value = arguments.get("query", arguments.get("smiles", arguments.get("input")))
        if not isinstance(value, str) or not value.strip():
            raise ToolInputError("tool requires a non-empty query string")
        return value.strip()

    def _execute_real(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        query = self._query(arguments)
        methods = {
            "MolSimilarity": self._similarity,
            "SMILES2Weight": self._weight,
            "FunctionalGroups": self._functional_groups,
            "Name2SMILES": self._name_to_smiles,
            "SMILES2Name": self._smiles_to_name,
            "Mol2CAS": self._mol_to_cas,
            "PatentCheck": self._patent_check,
            "SimilarityToControlChem": self._control_similarity,
            "ControlChemCheck": self._control_check,
            "ExplosiveCheck": self._explosive_check,
            "SafetySummary": self._safety_summary,
            "wikipedia": self._wikipedia,
            "WebSearch": self._web_search,
            "ReactionPredict": self._reaction_predict,
            "ReactionRetrosynthesis": self._reaction_retrosynthesis,
        }
        if tool_name in {"python_repl", "LiteratureSearch", "GetMoleculePrice"}:
            raise ToolUnavailableError(f"{tool_name} is excluded from the initial real profile")
        method = methods.get(tool_name)
        if method is None:
            raise ToolUnavailableError(f"unknown ChemCrow tool: {tool_name}")
        return method(query)

    def _require_network(self) -> None:
        if not self.network_enabled:
            raise ToolUnavailableError("public network tools are disabled by configuration")

    def _get_json(self, url: str) -> dict[str, Any]:
        self._require_network()
        response = httpx.get(
            url,
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "OpenEvo-ChemCrow/0.1 benchmark-integration"},
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("service returned a non-object JSON payload")
        return value

    def _similarity(self, query: str) -> dict[str, Any]:
        parts = query.split(".")
        if len(parts) != 2:
            raise ToolInputError("input exactly two SMILES separated by '.'")
        Chem, DataStructs, helpers = _rdkit()
        AllChem, _ = helpers
        molecules = [Chem.MolFromSmiles(part) for part in parts]
        if any(item is None for item in molecules):
            raise ToolInputError("invalid SMILES string")
        fingerprints = [AllChem.GetMorganGenerator(radius=2, fpSize=2048).GetFingerprint(m) for m in molecules]
        return {"tanimoto": DataStructs.TanimotoSimilarity(*fingerprints)}

    def _weight(self, query: str) -> dict[str, Any]:
        _, _, helpers = _rdkit()
        _, descriptors = helpers
        return {"exact_molecular_weight": descriptors.CalcExactMolWt(_molecule(query))}

    def _functional_groups(self, query: str) -> dict[str, Any]:
        Chem, _, _ = _rdkit()
        molecule = _molecule(query)
        groups = [
            name
            for name, smarts in _FUNCTIONAL_GROUPS.items()
            if molecule.HasSubstructMatch(Chem.MolFromSmarts(smarts))
        ]
        return {"functional_groups": groups}

    def _name_to_smiles(self, query: str) -> dict[str, Any]:
        data = self._get_json(
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
            f"{quote(query, safe='')}/property/IsomericSMILES/JSON"
        )
        try:
            value = data["PropertyTable"]["Properties"][0]["SMILES"]
        except (KeyError, IndexError, TypeError):
            try:
                value = data["PropertyTable"]["Properties"][0]["IsomericSMILES"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ToolInputError("PubChem did not return a matching molecule") from exc
        Chem, _, _ = _rdkit()
        molecule = Chem.MolFromSmiles(value)
        if molecule is None:
            raise RuntimeError("PubChem returned invalid SMILES")
        return {"smiles": Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)}

    def _smiles_to_name(self, query: str) -> dict[str, Any]:
        _molecule(query)
        data = self._get_json(
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/smiles/"
            f"{quote(query, safe='')}/synonyms/JSON"
        )
        try:
            names = data["InformationList"]["Information"][0]["Synonym"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ToolInputError("PubChem did not return a compound name") from exc
        name = next((item for item in names if not re.fullmatch(r"\d{2,7}-\d{2}-\d", item)), None)
        if not name:
            raise ToolInputError("PubChem returned no non-CAS name")
        return {"name": name}

    def _cid_and_view(self, query: str) -> tuple[int, dict[str, Any]]:
        mode = "smiles" if self._is_smiles(query) else "name"
        cids = self._get_json(
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/"
            f"{mode}/{quote(query, safe='')}/cids/JSON"
        )
        try:
            cid = int(cids["IdentifierList"]["CID"][0])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ToolInputError("PubChem did not return a CID") from exc
        view = self._get_json(
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON"
        )
        return cid, view

    def _mol_to_cas(self, query: str) -> dict[str, Any]:
        cid, data = self._cid_and_view(query)
        strings = _all_strings(data)
        cas = next((value for value in strings if re.fullmatch(r"\d{2,7}-\d{2}-\d", value)), None)
        if cas is None:
            raise ToolInputError("CAS number not found in PubChem record")
        return {"cid": cid, "cas": cas}

    def _patent_check(self, query: str) -> dict[str, Any]:
        try:
            import molbloom
        except ImportError as exc:
            raise ToolUnavailableError("molbloom is not installed") from exc
        _molecule(query)
        return {"surechembl_membership": bool(molbloom.buy(query, canonicalize=True, catalog="surechembl"))}

    def _controlled_rows(self) -> list[tuple[str, str]]:
        path = self.controlled_chemicals_csv
        if path is None or not path.is_file():
            raise ToolUnavailableError("controlled-chemical CSV is not bound")
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return [(str(row.get("cas", "")), str(row.get("smiles", ""))) for row in rows]

    def _control_similarity(self, query: str) -> dict[str, Any]:
        Chem, DataStructs, helpers = _rdkit()
        AllChem, _ = helpers
        molecule = _molecule(query)
        generator = AllChem.GetMorganGenerator(radius=2, fpSize=2048)
        target = generator.GetFingerprint(molecule)
        maximum = 0.0
        for _, smiles in self._controlled_rows():
            other = Chem.MolFromSmiles(smiles)
            if other is not None:
                maximum = max(maximum, DataStructs.TanimotoSimilarity(target, generator.GetFingerprint(other)))
        return {"maximum_similarity": maximum, "high_similarity": maximum > 0.35}

    def _control_check(self, query: str) -> dict[str, Any]:
        rows = self._controlled_rows()
        if self._is_smiles(query):
            Chem, _, _ = _rdkit()
            target = Chem.MolToSmiles(_molecule(query), canonical=True)
            exact = any(
                Chem.MolFromSmiles(smiles) is not None
                and Chem.MolToSmiles(Chem.MolFromSmiles(smiles), canonical=True) == target
                for _, smiles in rows
            )
            similarity = self._control_similarity(query)
        else:
            exact = any(query == cas.strip("()") for cas, _ in rows)
            similarity = None
        return {"exact_controlled_match": exact, "similarity": similarity}

    def _explosive_check(self, query: str) -> dict[str, Any]:
        _, data = self._cid_and_view(query)
        strings = _all_strings(data)
        matches = [item for item in strings if "explos" in item.lower()][:20]
        return {"explosive_classification_found": bool(matches), "evidence": matches}

    def _safety_summary(self, query: str) -> dict[str, Any]:
        cid, data = self._cid_and_view(query)
        strings = _all_strings(data)
        needles = ("hazard", "toxic", "warning", "danger", "environment", "explos")
        evidence = [
            item[:500]
            for item in strings
            if any(needle in item.lower() for needle in needles)
        ][:40]
        return {
            "cid": cid,
            "evidence": evidence,
            "note": "deterministic PubChem evidence extract; no hidden summarizer model was called",
        }

    def _wikipedia(self, query: str) -> dict[str, Any]:
        data = self._get_json(
            "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(query, safe="")
        )
        return {"title": data.get("title"), "extract": data.get("extract")}

    def _web_search(self, query: str) -> dict[str, Any]:
        key = os.environ.get("SERP_API_KEY")
        if not key:
            raise ToolUnavailableError("SERP_API_KEY is required for WebSearch")
        self._require_network()
        response = httpx.get(
            "https://serpapi.com/search.json",
            params={"q": query, "api_key": key, "engine": "google"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        return {"organic_results": data.get("organic_results", [])[:5]}

    def _reaction_predict(self, query: str) -> Any:
        _molecule(query)
        url = os.environ.get("CHEMCROW_RXN_PREDICT_URL")
        if url:
            return self._post_local_rxn(url, query, retrosynthesis=False)
        return self._hosted_rxn(query, retrosynthesis=False)

    def _reaction_retrosynthesis(self, query: str) -> Any:
        _molecule(query)
        url = os.environ.get("CHEMCROW_RXN_RETRO_URL")
        if url:
            return self._post_local_rxn(url, query, retrosynthesis=True)
        return self._hosted_rxn(query, retrosynthesis=True)

    def _post_local_rxn(self, url: str, query: str, *, retrosynthesis: bool) -> Any:
        self._require_network()
        payload: dict[str, Any]
        if retrosynthesis:
            payload = {
                "product": query,
                "topn": 3,
                "num_beams": 3,
                "fap": 0.6,
                "fld": 0.2,
                "device": "cpu",
            }
        else:
            payload = {
                "reactants_list": [query],
                "topn": 3,
                "num_beams": 3,
                "device": "cpu",
            }
        timeout = float(os.environ.get("CHEMCROW_RXN_TIMEOUT_SECONDS", "660"))
        # This adapter path is explicitly for the self-hosted/local RXN
        # service; host proxy configuration must not intercept loopback RPC.
        response = httpx.post(url, json=payload, timeout=timeout, trust_env=False)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise TypeError("local RXN returned a non-object JSON payload")
        if result.get("status") != "success":
            detail = result.get("message", result.get("detail", "unknown RXN failure"))
            raise RuntimeError(f"local RXN status={result.get('status')!r}: {detail}")
        return result

    def _hosted_rxn(self, query: str, *, retrosynthesis: bool) -> Any:
        key = os.environ.get("RXN4CHEM_API_KEY")
        project = os.environ.get("RXN4CHEM_PROJECT_ID")
        if not key or not project:
            raise ToolUnavailableError(
                "RXN4CHEM_API_KEY and RXN4CHEM_PROJECT_ID are required for hosted RXN"
            )
        if importlib.util.find_spec("rxn4chemistry") is None:
            raise ToolUnavailableError("install the hosted-rxn extra for IBM RXN")
        from rxn4chemistry import RXN4ChemistryWrapper

        base_url = os.environ.get("RXN4CHEMISTRY_BASE_URL", "https://rxn.res.ibm.com")
        wrapper = RXN4ChemistryWrapper(api_key=key, base_url=base_url)
        wrapper.project_id = project
        if retrosynthesis:
            response = wrapper.predict_automatic_retrosynthesis(product=query, max_steps=3)
            return {
                "prediction_id": response.get("prediction_id"),
                "status": "submitted",
                "note": "polling is intentionally delegated; no hidden recipe summarizer is called",
            }
        response = wrapper.predict_reaction(query)
        prediction_id = response.get("prediction_id")
        if not prediction_id:
            raise RuntimeError("RXN did not return prediction_id")
        result = wrapper.get_predict_reaction_results(prediction_id)
        return {"prediction_id": prediction_id, "result": result}

    @staticmethod
    def _is_smiles(value: str) -> bool:
        try:
            _molecule(value)
            return True
        except (ToolInputError, ToolUnavailableError):
            return False


def _all_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, str):
        if value.strip():
            strings.append(value.strip())
    elif isinstance(value, dict):
        for item in value.values():
            strings.extend(_all_strings(item))
    elif isinstance(value, list):
        for item in value:
            strings.extend(_all_strings(item))
    return strings


def environment_presence(names: list[str]) -> dict[str, bool]:
    """Return names and presence flags only; values are never read back or logged."""

    return {name: bool(os.environ.get(name)) for name in names}
