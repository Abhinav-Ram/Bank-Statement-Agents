-- Phase 2 schema. Idempotent: applied by app.db.migrate() at startup and before ingestion.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS categories (
    id    SERIAL PRIMARY KEY,
    head  TEXT NOT NULL CHECK (head IN ('Income', 'Expense')),
    name  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS statements (
    id                 TEXT PRIMARY KEY,               -- also the LangGraph thread id
    file_name          TEXT NOT NULL,
    file_sha256        TEXT NOT NULL UNIQUE,
    bank               TEXT,
    account_holder     TEXT,
    account_masked     TEXT,                           -- last 4 only
    period_start       DATE,
    period_end         DATE,
    opening_balance    BIGINT,                         -- paise
    closing_balance    BIGINT,
    currency           TEXT DEFAULT 'INR',
    source_type        TEXT CHECK (source_type IN ('digital', 'scanned', 'mixed')),
    status             TEXT NOT NULL CHECK (status IN ('processing', 'awaiting_review', 'ok', 'needs_ocr', 'failed')),
    reconciliation     JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transactions (
    id                   BIGSERIAL PRIMARY KEY,
    statement_id         TEXT NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    idx                  INT NOT NULL,                  -- order within the statement
    page                 INT,
    bbox                 REAL[],                        -- page points (scans: for the review crop)
    date                 DATE,
    value_date           DATE,
    description          TEXT NOT NULL DEFAULT '',
    merchant             TEXT,
    ref                  TEXT,
    debit                BIGINT NOT NULL DEFAULT 0,     -- paise
    credit               BIGINT NOT NULL DEFAULT 0,
    balance              BIGINT,
    category_id          INT REFERENCES categories(id),
    category_confidence  REAL,
    category_source      TEXT,                          -- rule | cache | llm | user
    source               TEXT,                          -- parser | ocr | ocr_* | llm
    notes                TEXT[] NOT NULL DEFAULT '{}',
    user_corrected       BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (statement_id, idx)
);
CREATE INDEX IF NOT EXISTS transactions_stmt_date ON transactions (statement_id, date);
CREATE INDEX IF NOT EXISTS transactions_merchant_trgm ON transactions USING gin (merchant gin_trgm_ops);

CREATE TABLE IF NOT EXISTS merchant_cache (
    merchant     TEXT PRIMARY KEY,                      -- normalised merchant key (upper case)
    category_id  INT NOT NULL REFERENCES categories(id),
    confidence   REAL NOT NULL DEFAULT 1.0,
    source       TEXT NOT NULL,                         -- rule | llm | user
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS anomalies (
    id              BIGSERIAL PRIMARY KEY,
    statement_id    TEXT NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    transaction_id  BIGINT REFERENCES transactions(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    score           REAL NOT NULL,
    details         JSONB,
    explanation     TEXT
);

CREATE TABLE IF NOT EXISTS summaries (
    statement_id  TEXT PRIMARY KEY REFERENCES statements(id) ON DELETE CASCADE,
    period        TEXT,
    fact_sheet    JSONB NOT NULL,
    narrative     TEXT,
    narrative_source TEXT                               -- llm | llm_regenerated | template
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id            BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    model         TEXT,
    ocr_engine    TEXT,
    dataset_tier  TEXT,
    metrics       JSONB NOT NULL
);

-- ---------------------------------------------------------------- Phase 3: retrieval index
CREATE TABLE IF NOT EXISTS chunks (
    id              BIGSERIAL PRIMARY KEY,
    statement_id    TEXT NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    transaction_id  BIGINT REFERENCES transactions(id) ON DELETE CASCADE,   -- NULL for summaries
    kind            TEXT NOT NULL CHECK (kind IN ('txn', 'month_summary', 'category_summary',
                                                  'anomaly', 'narrative')),
    text            TEXT NOT NULL,
    embedding       vector(384) NOT NULL,
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    metadata        JSONB NOT NULL DEFAULT '{}'                -- date, month, category, amount, ...
);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_text_trgm ON chunks USING gin (text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS chunks_stmt_kind ON chunks (statement_id, kind);

-- ---------------------------------------------------------------- duplicates across statements
-- A transaction already stored from another statement of the same account (same statement
-- uploaded twice, a scan of a saved PDF, overlapping periods) is kept but points at the original.
-- Every reader skips these rows, so nothing is counted twice. Deleting the original turns the
-- copy back into an original (SET NULL).
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS
    duplicate_of BIGINT REFERENCES transactions(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS transactions_duplicate_of ON transactions (duplicate_of);
ALTER TABLE statements ADD COLUMN IF NOT EXISTS dedupe JSONB;
CREATE OR REPLACE VIEW transactions_unique AS
    SELECT * FROM transactions WHERE duplicate_of IS NULL;

-- Free-form SQL fallback runs as this role (SET LOCAL ROLE) inside a READ ONLY transaction:
-- SELECT on the allowlisted tables only, so even a query that slipped past the validator can
-- neither write nor read anything else.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'bank_ro') THEN
        CREATE ROLE bank_ro NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO bank_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM bank_ro;
GRANT SELECT ON transactions_unique, categories, statements, anomalies, summaries TO bank_ro;

-- ---------------------------------------------------------------- Phase 4: API jobs + progress
-- One row per uploaded file (id = statement id = LangGraph thread id). Exists before the
-- statement is saved, so the UI can show queued / running / paused uploads.
CREATE TABLE IF NOT EXISTS jobs (
    statement_id  TEXT PRIMARY KEY,
    file_name     TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('queued', 'running', 'paused', 'done', 'failed',
                                                  'duplicate')),
    stage         TEXT,                                   -- last node that ran
    cloud_llm     BOOLEAN NOT NULL,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Progress events, replayable by id (SSE Last-Event-ID).
CREATE TABLE IF NOT EXISTS ingest_events (
    id            BIGSERIAL PRIMARY KEY,
    statement_id  TEXT NOT NULL,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind          TEXT NOT NULL,                          -- queued|node|paused|resumed|done|...
    data          JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ingest_events_stmt ON ingest_events (statement_id, id);
