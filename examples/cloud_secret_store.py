"""Managed secret-store example. It never prints the plaintext value."""

from __future__ import annotations

import os

from protoprompt.integrations import AWSSecretsManagerStore, GCPSecretManagerStore


def main() -> None:
    backend = os.environ.get("CLOUD_SECRET_BACKEND", "aws").lower()
    if backend == "aws":
        store = AWSSecretsManagerStore(
            prefix=os.environ.get("AWS_SECRET_PREFIX", "protoprompt-example"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
    elif backend == "gcp":
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            raise SystemExit("GOOGLE_CLOUD_PROJECT is required for GCP")
        store = GCPSecretManagerStore(project, prefix="protoprompt-example")
    else:
        raise SystemExit("CLOUD_SECRET_BACKEND must be aws or gcp")

    scope = os.environ.get("SECRET_SCOPE", "demo:user")
    key = os.environ.get("SECRET_KEY", "example-token")
    value = os.environ.get("SECRET_VALUE")
    if not value:
        raise SystemExit("set SECRET_VALUE; the example will store then delete it")
    try:
        store.put(key, value, scope=scope, ttl=300)
        print("stored:", store.get(key, scope=scope) is not None)
        print("keys:", store.list_keys(scope=scope))
        store.delete(key, scope=scope)
        print("deleted:", store.get(key, scope=scope) is None)
    finally:
        store.close()


if __name__ == "__main__":
    main()
