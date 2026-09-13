"""Executable freeze checks for the narrow ProtoPrompt v1 API candidate."""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import fields, is_dataclass
from importlib.resources import files

import protoprompt.api as api


def _resolve(path: str):
    module_name, _, attribute = path.rpartition(".")
    return getattr(importlib.import_module(module_name), attribute)


def test_manifest_identity_hash_and_detached_reads():
    raw = files("protoprompt").joinpath("api_contract_v1.json").read_bytes()
    decoded = json.loads(raw)
    canonical = json.dumps(
        decoded,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == api.V1_API_MANIFEST_SHA256

    first = api.v1_api_manifest()
    second = api.v1_api_manifest()
    assert first == second
    assert first is not second
    first["compatibility"]["private_rule"] = "mutated"
    assert api.v1_api_manifest() == second
    assert decoded == second


def test_manifest_and_module_freeze_the_same_exact_export_surface():
    manifest = api.v1_api_manifest()
    manifest_names = [entry["name"] for entry in manifest["exports"]]

    assert manifest["schema_version"] == api.API_CONTRACT_SCHEMA_VERSION == 1
    assert manifest["contract_id"] == api.API_CONTRACT_ID
    assert manifest["target_major"] == api.API_TARGET_MAJOR == 1
    assert manifest["status"] == api.V1_API_STATUS == "v1_candidate"
    assert manifest_names == api.__all__
    assert len(manifest_names) == len(set(manifest_names))

    for entry in manifest["exports"]:
        exported = getattr(api, entry["name"])
        implemented = _resolve(entry["implementation"])
        if entry["kind"] == "constant":
            assert exported == implemented
        else:
            assert exported is implemented


def test_frozen_public_fields_and_members_match_runtime_types():
    contracts = api.v1_api_manifest()["contracts"]
    for name, contract in contracts.items():
        runtime_type = getattr(api, name)
        if "public_fields" in contract:
            assert is_dataclass(runtime_type), name
            public_fields = [
                field.name for field in fields(runtime_type)
                if not field.name.startswith("_")
            ]
            assert public_fields == contract["public_fields"], name
        for member in contract["public_members"]:
            assert not member.startswith("_")
            assert hasattr(runtime_type, member), f"{name}.{member}"


def test_frozen_enum_values_match_runtime_order_and_spelling():
    for name, expected_values in api.v1_api_manifest()["enum_values"].items():
        assert [member.value for member in getattr(api, name)] == expected_values


def test_experimental_namespaces_are_explicit_and_outside_the_v1_seam():
    manifest = api.v1_api_manifest()
    namespaces = {
        entry["namespace"] for entry in manifest["experimental_namespaces"]
    }
    assert {
        "protoprompt.agent",
        "protoprompt.ledger.admission",
        "protoprompt.ledger.recall",
        "protoprompt.ledger.task_resume",
        "protoprompt.integrations",
        "apps",
    } <= namespaces

    implementations = [entry["implementation"] for entry in manifest["exports"]]
    for namespace in namespaces:
        assert not any(
            implementation == namespace
            or implementation.startswith(f"{namespace}.")
            for implementation in implementations
        ), namespace


def test_builtin_backend_operational_boundary_is_visible_on_both_classes():
    sqlite_members = {
        "storage_capabilities", "dry_run_setup", "setup", "schema_version",
        "backup", "close",
    }
    postgres_members = {
        "storage_capabilities", "dry_run_setup", "setup", "schema_version",
        "schema", "close",
    }
    assert sqlite_members <= set(dir(api.SqliteMemoryLedger))
    assert postgres_members <= set(dir(api.PostgresMemoryLedger))


def test_api_import_and_manifest_need_no_optional_sdk(monkeypatch):
    optional_roots = {
        "anthropic", "boto3", "chromadb", "elasticsearch", "fastapi",
        "google", "httpx", "langgraph", "llama_index", "openai",
        "opensearchpy", "psycopg", "qdrant_client", "redis",
        "sentence_transformers", "tiktoken",
    }
    imported = set()
    real_import = __import__

    def guarded_import(name, *args, **kwargs):
        root = name.partition(".")[0]
        if root in optional_roots:
            imported.add(root)
            raise AssertionError(f"optional SDK imported by protoprompt.api: {root}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    reloaded = importlib.reload(api)
    assert reloaded.v1_api_manifest()["contract_id"] == "protoprompt.public-api"
    assert imported == set()
