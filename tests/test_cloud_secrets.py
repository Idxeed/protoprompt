from __future__ import annotations

import builtins
from types import SimpleNamespace

import pytest

from protoprompt.integrations._cloud_secret import CloudSecretDataError
from protoprompt.integrations.aws_secrets import AWSSecretsManagerStore
from protoprompt.integrations.gcp_secrets import GCPSecretManagerStore
from protoprompt.secrets import SecretStore
from protoprompt.testing import check_secret_store


class AWSError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeAWSPaginator:
    def __init__(self, client) -> None:
        self.client = client

    def paginate(self, *, Filters):
        prefix = Filters[0]["Values"][0]
        names = [
            {"Name": name} for name in self.client.values
            if name.startswith(prefix) and name not in self.client.scheduled
        ]
        midpoint = len(names) // 2
        return [{"SecretList": names[:midpoint]}, {"SecretList": names[midpoint:]}]


class FakeAWSClient:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.scheduled: set[str] = set()
        self.closed = False
        self.deleted_with: dict | None = None

    def put_secret_value(self, *, SecretId, SecretString):
        if SecretId in self.scheduled:
            raise AWSError("InvalidRequestException", "Secret is scheduled for deletion")
        if SecretId not in self.values:
            raise AWSError("ResourceNotFoundException")
        self.values[SecretId] = SecretString

    def create_secret(self, *, Name, SecretString, Tags):
        if Name in self.values:
            raise AWSError("ResourceExistsException")
        self.values[Name] = SecretString

    def restore_secret(self, *, SecretId):
        self.scheduled.discard(SecretId)

    def get_secret_value(self, *, SecretId):
        if SecretId in self.scheduled:
            raise AWSError("InvalidRequestException", "Secret is scheduled for deletion")
        if SecretId not in self.values:
            raise AWSError("ResourceNotFoundException")
        return {"SecretString": self.values[SecretId]}

    def delete_secret(self, **kwargs):
        self.deleted_with = kwargs
        name = kwargs["SecretId"]
        if name not in self.values:
            raise AWSError("ResourceNotFoundException")
        if kwargs.get("ForceDeleteWithoutRecovery"):
            del self.values[name]
        else:
            self.scheduled.add(name)

    def get_paginator(self, operation):
        assert operation == "list_secrets"
        return FakeAWSPaginator(self)

    def close(self):
        self.closed = True


class NotFound(Exception):
    pass


class AlreadyExists(Exception):
    pass


class FakeGCPClient:
    def __init__(self) -> None:
        self.values: dict[str, list[bytes]] = {}
        self.created_requests: list[dict] = []
        self.closed = False

    def create_secret(self, *, request):
        name = request["parent"] + "/secrets/" + request["secret_id"]
        if name in self.values:
            raise AlreadyExists()
        self.values[name] = []
        self.created_requests.append(request)
        return SimpleNamespace(name=name)

    def add_secret_version(self, *, request):
        name = request["parent"]
        if name not in self.values:
            raise NotFound()
        self.values[name].append(request["payload"]["data"])

    def access_secret_version(self, *, request):
        name = request["name"].removesuffix("/versions/latest")
        if name not in self.values or not self.values[name]:
            raise NotFound()
        return SimpleNamespace(payload=SimpleNamespace(data=self.values[name][-1]))

    def delete_secret(self, *, request):
        if request["name"] not in self.values:
            raise NotFound()
        del self.values[request["name"]]

    def list_secrets(self, *, request):
        fragment = request["filter"].removeprefix("name:")
        return [
            SimpleNamespace(name=name)
            for name in self.values
            if fragment in name.rsplit("/", 1)[-1]
        ]

    def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "store",
    [
        AWSSecretsManagerStore(client=FakeAWSClient()),
        GCPSecretManagerStore("demo-project", client=FakeGCPClient()),
    ],
)
async def test_cloud_secret_stores_pass_contract(store):
    assert isinstance(store, SecretStore)
    report = await check_secret_store(store)
    assert report.contract == "secret_store"


@pytest.mark.parametrize("backend", ["aws", "gcp"])
def test_cloud_secret_scope_ttl_and_opaque_resource_names(backend):
    now = [100.0]
    if backend == "aws":
        client = FakeAWSClient()
        store = AWSSecretsManagerStore(client=client, clock=lambda: now[0])
    else:
        client = FakeGCPClient()
        store = GCPSecretManagerStore(
            "demo-project", client=client, clock=lambda: now[0]
        )

    store.put("github-token", "super-secret", scope="tenant/alice", ttl=10)
    resource_name = next(iter(client.values))
    assert "tenant" not in resource_name
    assert "alice" not in resource_name
    assert "github" not in resource_name
    assert "super-secret" not in resource_name
    assert store.get("github-token", scope="tenant/alice") == "super-secret"
    assert store.get("github-token", scope="tenant/bob") is None
    assert store.list_keys(scope="tenant/alice") == ["github-token"]

    now[0] = 111.0
    assert store.get("github-token", scope="tenant/alice") is None
    assert store.list_keys(scope="tenant/alice") == []


def test_aws_recoverable_delete_can_be_restored_by_put():
    client = FakeAWSClient()
    store = AWSSecretsManagerStore(client=client)
    store.put("token", "first", scope="acme")
    store.delete("token", scope="acme")
    assert client.deleted_with["RecoveryWindowInDays"] == 7
    assert store.get("token", scope="acme") is None
    store.put("token", "restored", scope="acme")
    assert store.get("token", scope="acme") == "restored"


def test_aws_force_delete_is_explicit():
    client = FakeAWSClient()
    store = AWSSecretsManagerStore(client=client, force_delete_without_recovery=True)
    store.put("token", "value", scope="acme")
    store.delete("token", scope="acme")
    assert client.deleted_with["ForceDeleteWithoutRecovery"] is True


def test_gcp_replication_and_versioning_are_explicit():
    client = FakeGCPClient()
    replication = {"user_managed": {"replicas": [{"location": "europe-west1"}]}}
    store = GCPSecretManagerStore(
        "demo-project", client=client, replication=replication
    )
    store.put("token", "one", scope="acme")
    store.put("token", "two", scope="acme")
    assert client.created_requests[0]["secret"]["replication"] == replication
    assert len(next(iter(client.values.values()))) == 2
    assert store.get("token", scope="acme") == "two"


@pytest.mark.parametrize(
    "store",
    [
        AWSSecretsManagerStore(client=FakeAWSClient()),
        GCPSecretManagerStore("demo-project", client=FakeGCPClient()),
    ],
)
def test_cloud_secret_validation_and_injected_lifecycle(store):
    with pytest.raises(ValueError, match="non-empty"):
        store.put("", "value", scope="scope")
    with pytest.raises(TypeError, match="string"):
        store.put("key", b"bytes", scope="scope")
    with pytest.raises(ValueError, match="positive"):
        store.put("key", "value", scope="scope", ttl=0)
    with pytest.raises(ValueError, match="64 KiB"):
        store.put("key", "x" * 70_000, scope="scope")
    store.close()
    assert store.client.closed is False


def test_corrupt_cloud_payload_fails_closed():
    client = FakeAWSClient()
    store = AWSSecretsManagerStore(client=client)
    name = store._name("token", "scope")
    client.values[name] = "not-json"
    with pytest.raises(CloudSecretDataError, match="invalid payload"):
        store.get("token", scope="scope")


def test_cloud_store_constructor_validation():
    with pytest.raises(ValueError, match="prefix"):
        AWSSecretsManagerStore(client=FakeAWSClient(), prefix="bad space")
    with pytest.raises(ValueError, match="between 7 and 30"):
        AWSSecretsManagerStore(client=FakeAWSClient(), recovery_window_days=3)
    with pytest.raises(ValueError, match="project_id"):
        GCPSecretManagerStore("bad/project", client=FakeGCPClient())
    with pytest.raises(ValueError, match="prefix"):
        GCPSecretManagerStore("demo", client=FakeGCPClient(), prefix="Bad")


def test_gcp_230_request_shapes_accept_adapter_payloads():
    pytest.importorskip("google.cloud.secretmanager_v1")
    from google.cloud.secretmanager_v1.types import resources, service

    created = service.CreateSecretRequest(
        parent="projects/demo",
        secret_id="protoprompt-s-abc-k-def",
        secret={
            "replication": {"automatic": {}},
            "labels": {"protoprompt": "managed"},
        },
    )
    version = service.AddSecretVersionRequest(
        parent="projects/demo/secrets/protoprompt-s-abc-k-def",
        payload={"data": b"payload"},
    )
    listed = service.ListSecretsRequest(
        parent="projects/demo",
        filter="name:protoprompt-s-abc-k-",
    )
    assert isinstance(created.secret, resources.Secret)
    assert version.payload.data == b"payload"
    assert listed.filter.startswith("name:")


@pytest.mark.parametrize("backend", ["aws", "gcp"])
def test_cloud_store_missing_extra_is_actionable(monkeypatch, backend):
    original_import = builtins.__import__

    def blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if backend == "aws" and name == "boto3":
            raise ImportError("blocked for contract")
        if backend == "gcp" and name == "google.cloud" and "secretmanager" in fromlist:
            raise ImportError("blocked for contract")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked)
    if backend == "aws":
        with pytest.raises(ImportError, match=r"protoprompt\[aws-secrets\]"):
            AWSSecretsManagerStore()
    else:
        with pytest.raises(ImportError, match=r"protoprompt\[gcp-secrets\]"):
            GCPSecretManagerStore("demo-project")
