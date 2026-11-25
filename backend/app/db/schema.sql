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
