"""Scaffold tests (C0.5): the package imports and its submodules exist.

These are deliberately trivial. They exist so CI has something real to run from day
one, and so a broken package layout fails fast rather than at first feature commit.
"""

from __future__ import annotations

import importlib
import re

import pytest

import purse

SUBMODULES = [
    "purse.gateway",
    "purse.auth",
    "purse.memory",
    "purse.skills",
    "purse.secrets",
    "purse.db",
]


def test_purse_imports() -> None:
    assert purse is not None


def test_version_is_a_semver_string() -> None:
    assert isinstance(purse.__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", purse.__version__), purse.__version__


@pytest.mark.parametrize("name", SUBMODULES)
def test_submodule_importable(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__name__ == name


@pytest.mark.parametrize("name", SUBMODULES)
def test_submodule_has_docstring(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__doc__, f"{name} is missing its module docstring"


def test_stdlib_secrets_is_not_shadowed() -> None:
    """purse.secrets must not shadow the stdlib `secrets` module for absolute imports."""
    stdlib_secrets = importlib.import_module("secrets")
    assert hasattr(stdlib_secrets, "token_urlsafe")
