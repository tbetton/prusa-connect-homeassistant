"""The integration imports the client library it bundles, or the installed one."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from custom_components.prusa_connect_ce import library

ROOT = Path(__file__).parent.parent
PACKAGE = "custom_components.prusa_connect_ce"


def test_uses_the_bundled_copy() -> None:
    """The integration runs on its bundled copy, not on an installed package."""
    assert library.PrusaConnect.__module__ == f"{PACKAGE}._vendor.prusa_connect.client"


def test_bundled_copy_matches_lib() -> None:
    """The bundled copy and manifest.json are what scripts/vendor_lib.py generates."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "vendor_lib.py"), "check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_falls_back_to_the_installed_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the bundled copy (a PyPI release) the installed package is used."""
    monkeypatch.syspath_prepend(str(ROOT / "lib" / "src"))
    for name in [name for name in sys.modules if name.startswith(f"{PACKAGE}._vendor.")]:
        monkeypatch.delitem(sys.modules, name)
    # A None entry makes the import of the bundled copy fail as if it were absent.
    monkeypatch.setitem(sys.modules, f"{PACKAGE}._vendor", None)  # type: ignore[arg-type]
    monkeypatch.delitem(sys.modules, f"{PACKAGE}.library")
    # Importing again rebinds the package attribute; put the original back afterwards.
    monkeypatch.setattr(sys.modules[PACKAGE], "library", library)
    for name in [name for name in sys.modules if name.split(".")[0] == "prusa_connect"]:
        monkeypatch.delitem(sys.modules, name)
    fallback = importlib.import_module(f"{PACKAGE}.library")
    assert fallback is not library
    assert fallback.PrusaConnect.__module__ == "prusa_connect.client"
