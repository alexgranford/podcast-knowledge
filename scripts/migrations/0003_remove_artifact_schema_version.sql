ALTER TABLE artifacts RENAME TO artifacts_with_legacy_version;

CREATE TABLE artifacts (
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

INSERT INTO artifacts(
    artifact_id, job_id, artifact_type, path, sha256, byte_size,
    producer_attempt_id, created_at
)
SELECT
    artifact_id, job_id, artifact_type, path, sha256, byte_size,
    producer_attempt_id, created_at
FROM artifacts_with_legacy_version;

DROP TABLE artifacts_with_legacy_version;