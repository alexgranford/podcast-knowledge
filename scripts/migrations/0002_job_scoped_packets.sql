ALTER TABLE packets RENAME TO packets_v1;

CREATE TABLE packets (
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
    PRIMARY KEY(job_id, packet_id, packet_version),
    UNIQUE(job_id, stage, payload_sha256)
);

INSERT INTO packets(
    packet_id, packet_version, job_id, stage, source_range_json, source_tokens,
    primary_units, overlap_units, prompt_tokens_estimate, output_tokens_predicted,
    payload_sha256, created_at
)
SELECT
    packet_id, packet_version, job_id, stage, source_range_json, source_tokens,
    primary_units, overlap_units, prompt_tokens_estimate, output_tokens_predicted,
    payload_sha256, created_at
FROM packets_v1;

DROP TABLE packets_v1;
