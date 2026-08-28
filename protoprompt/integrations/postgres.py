"""Async PostgreSQL/pgvector stores with explicit schema setup."""

from __future__ import annotations

import json
import math
from typing import Any

from protoprompt.profile.store import profile_from_dict, profile_to_dict
from protoprompt.profile.types import UserProfile


def _dependencies():
    try:
        from psycopg import sql
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:
        raise ImportError(
            "PostgreSQL adapters require psycopg 3 with pool support. "
            "Install with: pip install 'protoprompt[postgres]'"
        ) from exc
    return sql, AsyncConnectionPool


def _identifier(value: str, label: str) -> str:
    if not value or not value.replace("_", "a").isalnum() or value[0].isdigit():
        raise ValueError(f"{label} must be a non-empty SQL identifier")
    return value


def _vector_text(values: list[float], dimensions: int) -> str:
    if len(values) != dimensions:
        raise ValueError(
            f"embedding has {len(values)} dimensions; expected {dimensions}"
        )
    normalized = [float(value) for value in values]
    if not all(math.isfinite(value) for value in normalized):
        raise ValueError("embedding values must be finite")
    return "[" + ",".join(format(value, ".9g") for value in normalized) + "]"


class _AsyncPostgresOwner:
    def __init__(
        self,
        *,
        conninfo: str | None,
        pool: Any | None,
        min_pool_size: int,
        max_pool_size: int,
    ) -> None:
        _, AsyncConnectionPool = _dependencies()
        if (conninfo is None) == (pool is None):
            raise ValueError("provide exactly one of conninfo or pool")
        if pool is None:
            pool = AsyncConnectionPool(
                conninfo or "",
                min_size=min_pool_size,
                max_size=max_pool_size,
                open=False,
            )
            self._owns_pool = True
        else:
            self._owns_pool = False
        self._pool = pool

    @property
    def pool(self) -> Any:
        return self._pool

    async def open(self) -> None:
        """Open an owned pool explicitly; external pools remain host-owned."""
        if self._owns_pool and getattr(self._pool, "closed", False):
            await self._pool.open()

    def _require_open(self) -> None:
        if getattr(self._pool, "closed", False):
            raise RuntimeError("PostgreSQL pool is closed; call await store.open()")

    async def close(self) -> None:
        if self._owns_pool and not getattr(self._pool, "closed", False):
            await self._pool.close()

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


class PgVectorStore(_AsyncPostgresOwner):
    """Async-first pgvector implementation of ``AsyncStoreProtocol``.

    Construction and :meth:`open` never execute DDL. Applications or migration
    jobs must call :meth:`setup` explicitly before serving traffic.
    """

    MIGRATION_VERSION = 1

    def __init__(
        self,
        conninfo: str | None = None,
        *,
        pool: Any | None = None,
        dimensions: int,
        schema: str = "protoprompt",
        table: str = "memory_chunks",
        min_pool_size: int = 1,
        max_pool_size: int = 10,
    ) -> None:
        if dimensions < 1 or dimensions > 16_000:
            raise ValueError("dimensions must be between 1 and 16000")
        super().__init__(
            conninfo=conninfo,
            pool=pool,
            min_pool_size=min_pool_size,
            max_pool_size=max_pool_size,
        )
        self.dimensions = dimensions
        self.schema = _identifier(schema, "schema")
        self.table = _identifier(table, "table")

    async def setup(
        self,
        *,
        create_extension: bool = False,
        create_hnsw_index: bool = True,
    ) -> None:
        """Apply idempotent schema v1 after an explicit operator decision."""
        await self.open()
        self._require_open()
        sql, _ = _dependencies()
        relation = sql.Identifier(self.schema, self.table)
        migration_relation = sql.Identifier(self.schema, "schema_migrations")
        async with self._pool.connection() as connection:
            async with connection.transaction():
                if create_extension:
                    await connection.execute(
                        sql.SQL("CREATE EXTENSION IF NOT EXISTS vector")
                    )
                await connection.execute(sql.SQL(
                    "CREATE SCHEMA IF NOT EXISTS {}"
                ).format(sql.Identifier(self.schema)))
                await connection.execute(sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} ("
                "id BIGSERIAL PRIMARY KEY, "
                "doc_id TEXT NOT NULL, "
                "chunk_index INTEGER NOT NULL, "
                "document TEXT NOT NULL, "
                "metadata JSONB NOT NULL, "
                "embedding vector({}) NOT NULL, "
                "UNIQUE (doc_id, chunk_index))"
                ).format(relation, sql.SQL(str(self.dimensions))))
                await connection.execute(sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON {} (doc_id)"
                ).format(
                    sql.Identifier(f"{self.table}_doc_id_idx"),
                    relation,
                ))
                await connection.execute(sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} ("
                "component TEXT PRIMARY KEY, version INTEGER NOT NULL, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                ).format(migration_relation))
                await connection.execute(sql.SQL(
                "INSERT INTO {} (component, version) VALUES (%s, %s) "
                "ON CONFLICT (component) DO UPDATE SET "
                "version = EXCLUDED.version, applied_at = now()"
                ).format(migration_relation), (
                    f"pgvector:{self.table}",
                    self.MIGRATION_VERSION,
                ))
                if create_hnsw_index:
                    await connection.execute(sql.SQL(
                        "CREATE INDEX IF NOT EXISTS {} ON {} "
                        "USING hnsw (embedding vector_cosine_ops)"
                    ).format(
                        sql.Identifier(f"{self.table}_embedding_hnsw_idx"),
                        relation,
                    ))

    async def add(
        self,
        doc_id: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict | None = None,
    ) -> None:
        self._require_open()
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must have the same length")
        sql, _ = _dependencies()
        relation = sql.Identifier(self.schema, self.table)
        rows = [
            (
                str(doc_id),
                index,
                text,
                json.dumps(
                    {**(metadata or {}), "chunk_index": index, "doc_id": str(doc_id)},
                    ensure_ascii=False,
                ),
                _vector_text(embedding, self.dimensions),
            )
            for index, (text, embedding) in enumerate(zip(chunks, embeddings))
        ]
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    sql.SQL("DELETE FROM {} WHERE doc_id = %s").format(relation),
                    (str(doc_id),),
                )
                if rows:
                    async with connection.cursor() as cursor:
                        await cursor.executemany(
                            sql.SQL(
                                "INSERT INTO {} "
                                "(doc_id, chunk_index, document, metadata, embedding) "
                                "VALUES (%s, %s, %s, %s::jsonb, %s::vector)"
                            ).format(relation),
                            rows,
                        )

    async def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        where: dict | None = None,
        score_threshold: float | None = None,
    ) -> list[dict]:
        self._require_open()
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        vector = _vector_text(embedding, self.dimensions)
        sql, _ = _dependencies()
        relation = sql.Identifier(self.schema, self.table)
        clauses: list[Any] = []
        params: list[Any] = [vector]
        for key, condition in (where or {}).items():
            if not isinstance(key, str):
                raise TypeError("metadata filter keys must be strings")
            values = (
                condition.get("$in")
                if isinstance(condition, dict) and "$in" in condition
                else None
            )
            if values is not None:
                if not isinstance(values, (list, tuple, set)):
                    raise TypeError("$in filter value must be a sequence")
                if not values:
                    return []
                alternatives = []
                for value in values:
                    alternatives.append(sql.SQL("metadata -> %s = %s::jsonb"))
                    params.extend((key, json.dumps(value, ensure_ascii=False)))
                clauses.append(
                    sql.SQL("(")
                    + sql.SQL(" OR ").join(alternatives)
                    + sql.SQL(")")
                )
            else:
                clauses.append(sql.SQL("metadata -> %s = %s::jsonb"))
                params.extend((key, json.dumps(condition, ensure_ascii=False)))
        if score_threshold is not None:
            clauses.append(sql.SQL("1 - (embedding <=> %s::vector) >= %s"))
            params.extend((vector, float(score_threshold)))
        where_sql = (
            sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
            if clauses
            else sql.SQL("")
        )
        params.extend((vector, top_k))
        statement = sql.SQL(
            "SELECT id, document, metadata, "
            "1 - (embedding <=> %s::vector) AS score FROM {}{} "
            "ORDER BY embedding <=> %s::vector LIMIT %s"
        ).format(relation, where_sql)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(statement, tuple(params))
            rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "document": row[1],
                "metadata": dict(row[2]),
                "score": float(row[3]),
            }
            for row in rows
        ]

    async def get(self, doc_id: str) -> dict | None:
        self._require_open()
        sql, _ = _dependencies()
        statement = sql.SQL(
            "SELECT document, metadata FROM {} "
            "WHERE doc_id = %s ORDER BY chunk_index LIMIT 1"
        ).format(sql.Identifier(self.schema, self.table))
        async with self._pool.connection() as connection:
            cursor = await connection.execute(statement, (str(doc_id),))
            row = await cursor.fetchone()
        if row is None:
            return None
        return {"document": row[0], "metadata": dict(row[1])}

    async def delete(self, doc_id: str) -> None:
        self._require_open()
        sql, _ = _dependencies()
        async with self._pool.connection() as connection:
            await connection.execute(
                sql.SQL("DELETE FROM {} WHERE doc_id = %s").format(
                    sql.Identifier(self.schema, self.table)
                ),
                (str(doc_id),),
            )

    async def count(self) -> int:
        self._require_open()
        sql, _ = _dependencies()
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                sql.SQL("SELECT COUNT(*) FROM {}").format(
                    sql.Identifier(self.schema, self.table)
                )
            )
            row = await cursor.fetchone()
        return int(row[0])


class PostgresProfileStore(_AsyncPostgresOwner):
    """Async profile store with tenant isolation and compare-and-swap writes."""

    MIGRATION_VERSION = 1

    def __init__(
        self,
        conninfo: str | None = None,
        *,
        pool: Any | None = None,
        tenant: str = "default",
        schema: str = "protoprompt",
        table: str = "profiles",
        min_pool_size: int = 1,
        max_pool_size: int = 10,
    ) -> None:
        if not tenant:
            raise ValueError("tenant must not be empty")
        super().__init__(
            conninfo=conninfo,
            pool=pool,
            min_pool_size=min_pool_size,
            max_pool_size=max_pool_size,
        )
        self.tenant = tenant
        self.schema = _identifier(schema, "schema")
        self.table = _identifier(table, "table")

    async def setup(self) -> None:
        await self.open()
        self._require_open()
        sql, _ = _dependencies()
        relation = sql.Identifier(self.schema, self.table)
        migration_relation = sql.Identifier(self.schema, "schema_migrations")
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        sql.Identifier(self.schema)
                    )
                )
                await connection.execute(sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} ("
                    "tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, "
                    "profile JSONB NOT NULL, version INTEGER NOT NULL, "
                    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                    "PRIMARY KEY (tenant_id, user_id))"
                ).format(relation))
                await connection.execute(sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} ("
                    "component TEXT PRIMARY KEY, version INTEGER NOT NULL, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                ).format(migration_relation))
                await connection.execute(sql.SQL(
                    "INSERT INTO {} (component, version) VALUES (%s, %s) "
                    "ON CONFLICT (component) DO UPDATE SET "
                    "version = EXCLUDED.version, applied_at = now()"
                ).format(migration_relation), (
                    f"profiles:{self.table}",
                    self.MIGRATION_VERSION,
                ))

    async def get(self, user_id: str) -> UserProfile | None:
        self._require_open()
        sql, _ = _dependencies()
        async with self._pool.connection() as connection:
            cursor = await connection.execute(sql.SQL(
                "SELECT profile FROM {} WHERE tenant_id = %s AND user_id = %s"
            ).format(sql.Identifier(self.schema, self.table)), (
                self.tenant,
                str(user_id),
            ))
            row = await cursor.fetchone()
        return profile_from_dict(dict(row[0])) if row is not None else None

    async def put(self, profile: UserProfile) -> None:
        self._require_open()
        sql, _ = _dependencies()
        payload = json.dumps(profile_to_dict(profile), ensure_ascii=False)
        async with self._pool.connection() as connection:
            await connection.execute(sql.SQL(
                "INSERT INTO {} (tenant_id, user_id, profile, version) "
                "VALUES (%s, %s, %s::jsonb, %s) "
                "ON CONFLICT (tenant_id, user_id) DO UPDATE SET "
                "profile = EXCLUDED.profile, version = EXCLUDED.version, "
                "updated_at = now()"
            ).format(sql.Identifier(self.schema, self.table)), (
                self.tenant,
                profile.user_id,
                payload,
                profile.version,
            ))

    async def compare_and_put(
        self,
        profile: UserProfile,
        *,
        expected_version: int | None,
    ) -> bool:
        self._require_open()
        sql, _ = _dependencies()
        payload = json.dumps(profile_to_dict(profile), ensure_ascii=False)
        relation = sql.Identifier(self.schema, self.table)
        async with self._pool.connection() as connection:
            if expected_version is None:
                cursor = await connection.execute(sql.SQL(
                    "INSERT INTO {} (tenant_id, user_id, profile, version) "
                    "VALUES (%s, %s, %s::jsonb, %s) "
                    "ON CONFLICT (tenant_id, user_id) DO NOTHING"
                ).format(relation), (
                    self.tenant,
                    profile.user_id,
                    payload,
                    profile.version,
                ))
            else:
                cursor = await connection.execute(sql.SQL(
                    "UPDATE {} SET profile = %s::jsonb, version = %s, "
                    "updated_at = now() WHERE tenant_id = %s AND user_id = %s "
                    "AND version = %s"
                ).format(relation), (
                    payload,
                    profile.version,
                    self.tenant,
                    profile.user_id,
                    expected_version,
                ))
        return cursor.rowcount == 1

    async def delete(self, user_id: str) -> None:
        self._require_open()
        sql, _ = _dependencies()
        async with self._pool.connection() as connection:
            await connection.execute(sql.SQL(
                "DELETE FROM {} WHERE tenant_id = %s AND user_id = %s"
            ).format(sql.Identifier(self.schema, self.table)), (
                self.tenant,
                str(user_id),
            ))
