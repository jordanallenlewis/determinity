"""Unit tests for the fleet-wide max execution timeout setting.

The getter is tested in isolation from the settings DB (`get_setting` is
monkeypatched), like the max_parallel_tasks ceiling tests.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def ss():
    from services import settings_service as _ss
    return _ss


def _patch_raw(monkeypatch, ss, value):
    monkeypatch.setattr(ss.settings_service, "get_setting", lambda *_a, **_kw: value)


class TestMaxExecutionTimeoutGetter:
    def test_absent_keeps_the_two_hour_default(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, None)
        assert ss.get_max_execution_timeout() == 7200

    def test_raised_value_is_honoured(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, "86400")
        assert ss.get_max_execution_timeout() == 86400

    def test_garbage_falls_back_to_default(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, "soon")
        assert ss.get_max_execution_timeout() == ss.MAX_EXECUTION_TIMEOUT_DEFAULT

    def test_read_failure_falls_back_to_default(self, ss, monkeypatch):
        def _boom(*_a, **_kw):
            raise RuntimeError("db down")
        monkeypatch.setattr(ss.settings_service, "get_setting", _boom)
        assert ss.get_max_execution_timeout() == ss.MAX_EXECUTION_TIMEOUT_DEFAULT

    def test_below_min_clamps_to_min(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, "0")
        assert ss.get_max_execution_timeout() == ss.MAX_EXECUTION_TIMEOUT_MIN

    def test_above_max_clamps_to_seven_days(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, "99999999")
        assert ss.get_max_execution_timeout() == 604_800


class TestSetExecutionTimeoutFloorOnly:
    def test_db_layer_no_longer_caps_at_two_hours(self):
        import inspect
        from db.agent_settings import resources
        src = inspect.getsource(resources)
        assert "timeout_seconds > 7200" not in src
