"""Hermetic test setup: no network, no OTel exporters, no API keys."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LLM", "1")
    monkeypatch.setenv("REWIND_DISABLE_OTEL", "1")
    monkeypatch.setenv("REWIND_BACKEND", "mirror")
    monkeypatch.setenv("REWIND_MIRROR_PATH", str(tmp_path / "telemetry.jsonl"))
    monkeypatch.delenv("REWIND_MAX_FIELD_BYTES", raising=False)
    yield
