-- Frozen source-shape fixture from the v0.6.1 SqliteStore and
-- SqliteProfileStore implementations. It intentionally contains ordinary
-- vector, session, and profile data only: v0.6.1 had no Memory Ledger schema.

CREATE TABLE chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    document TEXT NOT NULL,
    metadata TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE INDEX idx_chunks_doc_id ON chunks(doc_id);

INSERT INTO chunks(doc_id, chunk_index, document, metadata, embedding) VALUES
    (
        'document-v06',
        0,
        'Legacy PDF policy: the source remains authoritative during cutover.',
        '{"chunk_index":0,"doc_id":"document-v06","kind":"document","name":"legacy-policy.pdf"}',
        X'0000803F00000000'
    ),
    (
        'session-v06',
        0,
        'Legacy session summary: do not auto-promote this text into Ledger memory.',
        '{"chat_id":"legacy-chat","chunk_index":0,"doc_id":"session-v06","kind":"session"}',
        X'000000000000803F'
    );

CREATE TABLE profiles (
    user_id TEXT PRIMARY KEY,
    json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);

INSERT INTO profiles(user_id, json, version) VALUES (
    'legacy-user',
    '{"user_id":"legacy-user","traits":{"style":"concise","expertise":"","verbosity":"","formality":""},"preferences":{"format":"bullets","language":"ru","topics":["террасы"]},"facts":{"city":"Химки"},"summary":"Legacy profile stays outside the Ledger until a host explicitly re-ingests it.","updated_at":"2026-08-01T00:00:00+00:00","version":3,"source":"legacy-rule"}',
    3
);
