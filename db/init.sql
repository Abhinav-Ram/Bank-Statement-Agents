-- Phase 0: extensions only. Tables land in Phase 2 (statements/transactions/...)
-- and Phase 3 (chunks, once the embedding dimension is fixed).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
