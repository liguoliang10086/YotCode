"""Shared pytest fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.yotcode directory."""
    home = tmp_path / "ccpy-home"
    home.mkdir()
    monkeypatch.setenv("CCPY_HOME", str(home))
    monkeypatch.delenv("CLAUDEPY_HOME", raising=False)
    return home
