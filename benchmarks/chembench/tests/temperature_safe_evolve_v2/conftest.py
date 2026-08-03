from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, ClassVar
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openevo import __version__
from openevo.evolution.framework import DistributionArtifactExpectation
from openevo.evolution.framework import builtins as core_builtins
from openevo.evolution.framework.builtins import load_verified_builtin_registry
from openevo.evolution.framework.loading import _verify_distribution_install


class _SourceDistribution:
    metadata: ClassVar[dict[str, str]] = {"Name": "openevo"}
    version = __version__

    def __init__(self, install_root: Path) -> None:
        self._install_root = install_root

    def locate_file(self, path: str) -> Path:
        return self._install_root / path

    def read_text(self, _filename: str) -> None:
        return None


@pytest.fixture(scope="session")
def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


@pytest.fixture(scope="session")
def executable_registry(tmp_path_factory: pytest.TempPathFactory) -> Any:
    temporary = tmp_path_factory.mktemp("safe-evolve-v2-registry")
    install_root = Path(core_builtins.__file__).resolve().parents[3]
    artifact = temporary / f"openevo-{__version__}-py3-none-any.whl"
    with ZipFile(artifact, "w", compression=ZIP_DEFLATED) as wheel:
        for path in sorted((install_root / "openevo").rglob("*")):
            if path.is_file() and path.name.endswith(
                (".py", ".pyi", ".so", ".pyd", ".dll", ".dylib")
            ):
                wheel.write(path, path.relative_to(install_root).as_posix())
        wheel.writestr(
            f"openevo-{__version__}.dist-info/METADATA",
            f"Name: openevo\nVersion: {__version__}\n",
        )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    verified = _verify_distribution_install(
        DistributionArtifactExpectation(
            distribution="openevo",
            distribution_version=__version__,
            distribution_digest=digest,
        ),
        artifact,
        metadata_provider=lambda _name: _SourceDistribution(install_root),
    )
    return load_verified_builtin_registry(verified)
