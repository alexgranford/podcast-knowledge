PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    source_identity TEXT NOT NULL UNIQUE,
    stage TEXT NOT NULL CHECK (stage IN (
        'queued','resolving','acquiring_audio','transcribing','validating_transcript',
        'extracting','consolidating','planning','drafting_sections','drafting_frame',
        'rendering','delivering','complete','blocked_needs_human','failed_terminal','cancelled'
    )),
    resume_stage TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    consecutive_failure_class TEXT,
    consecutive_failure_count INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failure_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    producer_attempt_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, artifact_type, path, sha256)
);

CREATE TABLE IF NOT EXISTS packets (
    packet_id TEXT NOT NULL,
    packet_version INTEGER NOT NULL CHECK (packet_version >= 1),
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    stage TEXT NOT NULL,
    source_range_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(source_range_json)),
    source_tokens INTEGER NOT NULL DEFAULT 0 CHECK (source_tokens >= 0),
    primary_units INTEGER NOT NULL DEFAULT 0 CHECK (primary_units >= 0),
    overlap_units INTEGER NOT NULL DEFAULT 0 CHECK (overlap_units >= 0),
    prompt_tokens_estimate INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens_estimate >= 0),
    output_tokens_predicted INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens_predicted >= 0),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(packet_id, packet_version),
    UNIQUE(job_id, stage, payload_sha256)
);

-- One append-only linear journal for action starts/outcomes, state decisions,
-- normalized failures and deterministic fallback use.
CREATE TABLE IF NOT EXISTS attempts (
    journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('start','success','failure','fallback_used','decision')),
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    stage_from TEXT,
    stage_to TEXT,
    packet_id TEXT NOT NULL DEFAULT '',
    packet_version INTEGER NOT NULL DEFAULT 0 CHECK (packet_version >= 0),
    operation TEXT,
    operation_class TEXT,
    payload_sha256 TEXT CHECK (payload_sha256 IS NULL OR length(payload_sha256) = 64),
    attempt_number INTEGER CHECK (attempt_number IS NULL OR attempt_number >= 1),
    deterministic INTEGER CHECK (deterministic IS NULL OR deterministic IN (0,1)),
    generation_started INTEGER CHECK (generation_started IS NULL OR generation_started IN (0,1)),
    model TEXT,
    prompt_budget INTEGER CHECK (prompt_budget IS NULL OR prompt_budget >= 0),
    completion_budget INTEGER CHECK (completion_budget IS NULL OR completion_budget >= 0),
    prompt_tokens INTEGER CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
    completion_tokens INTEGER CHECK (completion_tokens IS NULL OR completion_tokens >= 0),
    elapsed_ms INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    finish_reason TEXT,
    failure_class TEXT,
    failure_code TEXT,
    diagnostic TEXT,
    retryable INTEGER CHECK (retryable IS NULL OR retryable IN (0,1)),
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL,
    CHECK (
        (kind IN ('start','success','failure') AND attempt_id IS NOT NULL)
        OR kind IN ('fallback_used','decision')
    ),
    CHECK (kind = 'failure' OR (failure_class IS NULL AND failure_code IS NULL AND diagnostic IS NULL AND retryable IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS attempts_one_start
ON attempts(attempt_id) WHERE kind = 'start';

CREATE UNIQUE INDEX IF NOT EXISTS attempts_one_terminal_outcome
ON attempts(attempt_id) WHERE kind IN ('success','failure');

CREATE UNIQUE INDEX IF NOT EXISTS attempts_deterministic_generation_once
ON attempts(job_id, stage_to, packet_id, packet_version, operation, payload_sha256)
WHERE kind = 'start' AND deterministic = 1 AND generation_started = 1;

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    attempt_id TEXT,
    message_kind TEXT NOT NULL,
    observed_revision INTEGER NOT NULL CHECK (observed_revision >= 0),
    observed_stage TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    note_sha256 TEXT CHECK (note_sha256 IS NULL OR length(note_sha256) = 64),
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    UNIQUE(platform, platform_id),
    UNIQUE(job_id, message_kind, platform_id)
);

CREATE INDEX IF NOT EXISTS attempts_job_order ON attempts(job_id, journal_id);
CREATE INDEX IF NOT EXISTS attempts_job_kind ON attempts(job_id, kind, operation_class);
CREATE INDEX IF NOT EXISTS attempts_failure_class ON attempts(job_id, failure_class, journal_id) WHERE kind = 'failure';
CREATE INDEX IF NOT EXISTS deliveries_job_kind ON deliveries(job_id, message_kind, delivery_id);
