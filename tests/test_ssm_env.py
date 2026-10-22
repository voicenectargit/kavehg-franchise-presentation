"""Secrets come from SSM on Railway, and the boot refuses to run without them.

Run: python -m pytest tests/test_ssm_env.py
"""
import os
import sys
import types
from unittest import mock

import pytest

from app import ssm_env

PATH = "/ambivo/railway/content-portal/"


def _fake_boto3(params=None, error=None):
    class _Pager:
        def paginate(self, **kw):
            assert kw == {"Path": PATH, "WithDecryption": True, "Recursive": False}
            if error:
                raise error
            return [{"Parameters": [{"Name": PATH + k, "Value": v} for k, v in (params or {}).items()]}]

    class _Client:
        def get_paginator(self, name):
            assert name == "get_parameters_by_path"
            return _Pager()

    return types.SimpleNamespace(client=lambda svc, region_name=None: _Client())


def _run(env, **fake):
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.dict(sys.modules, {"boto3": _fake_boto3(**fake)}):
        n = ssm_env.load_ssm_into_environ()
        return n, dict(os.environ)


def test_unset_path_does_nothing():
    n, env = _run({"JWT_SECRET_KEY": "local"})
    assert n == 0 and env["JWT_SECRET_KEY"] == "local"


def test_parameters_land_in_the_environment():
    n, env = _run({"AMBIVO_SSM_PATH": PATH},
                  params={"JWT_SECRET_KEY": "j", "AMBIVO_INTERNAL_SECRET": "s"})
    assert n == 2
    assert env["JWT_SECRET_KEY"] == "j" and env["AMBIVO_INTERNAL_SECRET"] == "s"


def test_ssm_wins_over_a_value_left_on_railway(caplog):
    # A rotation done in SSM must reach the service even if Railway still holds the old one.
    n, env = _run({"AMBIVO_SSM_PATH": PATH, "JWT_SECRET_KEY": "old-leaked"},
                  params={"JWT_SECRET_KEY": "new"})
    assert env["JWT_SECRET_KEY"] == "new"
    assert "JWT_SECRET_KEY" in caplog.text
    assert "old-leaked" not in caplog.text and "new" not in caplog.text.replace("ssm_env", "")


def test_path_without_trailing_slash_is_read_the_same():
    n, env = _run({"AMBIVO_SSM_PATH": PATH.rstrip("/")}, params={"X": "1"})
    assert env["X"] == "1"


def test_a_failed_read_stops_the_boot():
    with pytest.raises(RuntimeError, match="Could not read secrets"):
        _run({"AMBIVO_SSM_PATH": PATH}, error=PermissionError("AccessDenied"))


def test_an_empty_path_stops_the_boot():
    with pytest.raises(RuntimeError, match="holds no parameters"):
        _run({"AMBIVO_SSM_PATH": PATH}, params={})
