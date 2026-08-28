from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from protoprompt.integrations.aws_secrets import AWSSecretsManagerStore
from protoprompt.integrations.gcp_secrets import GCPSecretManagerStore
from protoprompt.testing import check_secret_store

pytestmark = pytest.mark.integration


async def _contract_with_timeout(store, *, timeout: float = 120.0):
    # SDKs are synchronous by design; keep their network I/O off the event loop.
    return await asyncio.wait_for(
        asyncio.to_thread(lambda: asyncio.run(check_secret_store(store))),
        timeout=timeout,
    )


@pytest.mark.asyncio
async def test_aws_secrets_manager_live_contract():
    if os.environ.get("PROTOPROMPT_AWS_SECRETS_LIVE") != "1":
        pytest.skip("set PROTOPROMPT_AWS_SECRETS_LIVE=1 to create AWS test secrets")
    from botocore.config import Config

    prefix = "protoprompt-live/" + uuid.uuid4().hex
    store = AWSSecretsManagerStore(
        prefix=prefix,
        force_delete_without_recovery=True,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(connect_timeout=10, read_timeout=20, retries={"max_attempts": 2}),
    )
    try:
        report = await _contract_with_timeout(store)
        assert report.contract == "secret_store"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_gcp_secret_manager_live_contract():
    project_id = os.environ.get("PROTOPROMPT_GCP_SECRET_PROJECT")
    if not project_id:
        pytest.skip("set PROTOPROMPT_GCP_SECRET_PROJECT to create GCP test secrets")
    store = GCPSecretManagerStore(
        project_id,
        prefix="pplive" + uuid.uuid4().hex[:12],
    )
    try:
        report = await _contract_with_timeout(store)
        assert report.contract == "secret_store"
    finally:
        store.close()
