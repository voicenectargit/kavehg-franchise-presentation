# app/ssm_env.py
# Copyright (C) Ambivo Inc

"""
Secrets from AWS SSM Parameter Store, put into the environment before Settings reads it.

Railway leaked every service's environment variables in its 2026-10-21 breach.
Secrets now live in SSM under one path per Railway project, and Railway holds only
what it takes to read that path:

    AMBIVO_SSM_PATH         /ambivo/railway/content-portal/
    AWS_ACCESS_KEY_ID       an IAM user that can read only that path
    AWS_SECRET_ACCESS_KEY
    AWS_REGION              us-east-1

⚠ SSM WINS OVER THE ENVIRONMENT. A variable left behind on Railway after its
secret moved would otherwise shadow the SSM value, and a rotation done in SSM
would never reach this service. Each overridden name is logged (never the value).

⚠ FAILS CLOSED. With AMBIVO_SSM_PATH set, a failed read stops the boot: running
on whatever happens to be in the environment is how a service ends up on a stale
or leaked secret without anyone noticing. With it unset (local, tests) this does
nothing.
"""

import logging
import os

logger = logging.getLogger(__name__)


def load_ssm_into_environ() -> int:
    """Copy every parameter under AMBIVO_SSM_PATH into os.environ. Returns the count."""
    path = (os.environ.get("AMBIVO_SSM_PATH") or "").strip()
    if not path:
        return 0
    if not path.endswith("/"):
        path += "/"

    import boto3

    client = boto3.client("ssm", region_name=os.environ.get("AWS_REGION") or "us-east-1")
    loaded, overridden = 0, []
    try:
        pages = client.get_paginator("get_parameters_by_path").paginate(
            Path=path, WithDecryption=True, Recursive=False)
        for page in pages:
            for param in page.get("Parameters", []):
                name = param["Name"][len(path):]
                if not name:
                    continue
                if os.environ.get(name) not in (None, "", param["Value"]):
                    overridden.append(name)
                os.environ[name] = param["Value"]
                loaded += 1
    except Exception as ex:
        raise RuntimeError(f"Could not read secrets from SSM path {path}: "
                           f"{type(ex).__name__}: {ex}") from ex

    if not loaded:
        raise RuntimeError(f"SSM path {path} holds no parameters; refusing to boot without secrets")
    logger.warning("ssm_env: %s secret(s) loaded from %s", loaded, path)
    if overridden:
        logger.warning("ssm_env: SSM replaced environment value(s) for %s; "
                       "remove them from Railway", ", ".join(sorted(overridden)))
    return loaded
