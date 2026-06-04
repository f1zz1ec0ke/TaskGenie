"""Shared fixtures for TaskGenie tests."""
from __future__ import annotations

import pytest

from taskgenie.agent import Agent


@pytest.fixture(autouse=True)
def clean_agent_registry():
    """Ensure Agent.REGISTRY is clean before and after each test."""
    Agent.REGISTRY.clear()
    yield
    Agent.REGISTRY.clear()


@pytest.fixture
def tmp_workspace(tmp_path):
    """Provide a temporary workspace directory."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    Agent.WORKSPACE_ROOT = str(workspace)
    yield workspace
    Agent.WORKSPACE_ROOT = None


@pytest.fixture
def tmp_scratch(tmp_path):
    """Provide a temporary scratch directory."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    Agent.SCRATCH_ROOT = str(scratch)
    yield scratch
    Agent.SCRATCH_ROOT = None
