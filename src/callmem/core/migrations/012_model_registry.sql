-- callmem schema v12: Model registry (A5)
--
-- A registry of known LLM models with capabilities, pricing, context
-- windows, geo-restrictions, quality signals, and gateway availability.
-- Populated from gateway syncs, a research agent, and observed
-- performance data (model_stats).

CREATE TABLE IF NOT EXISTS model_registry (
    model_name          TEXT PRIMARY KEY,
    provider            TEXT,
    display_name        TEXT,
    pricing_input       REAL,
    pricing_output      REAL,
    context_window      INTEGER,
    max_output          INTEGER,
    supports_tools      INTEGER DEFAULT 0,
    supports_vision     INTEGER DEFAULT 0,
    supports_streaming  INTEGER DEFAULT 0,
    strengths           TEXT,
    weaknesses          TEXT,
    benchmarks          TEXT,
    latency_p50_ms      INTEGER,
    geo_available       TEXT,
    geo_blocked         TEXT,
    geo_notes           TEXT,
    quality_tier        TEXT,
    use_case_scores     TEXT,
    known_issues        TEXT,
    release_date        TEXT,
    deprecation_date    TEXT,
    gateways            TEXT,
    last_synced         TEXT,
    last_researched     TEXT,
    last_updated        TEXT NOT NULL,
    metadata            TEXT
);
