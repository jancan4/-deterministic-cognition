CREATE TABLE IF NOT EXISTS memory_schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type       TEXT    NOT NULL CHECK (event_type IN (
                         'architecture_decision','governance_rule','hypothesis','experiment',
                         'validation_result','adaptation','regime_observation',
                         'implementation_note','open_question','rejected_idea',
                         'incident','source_reference'
                     )),
    title            TEXT    NOT NULL CHECK (title != ''),
    summary          TEXT    NOT NULL CHECK (summary != ''),
    evidence         TEXT,
    source           TEXT    NOT NULL CHECK (source != ''),
    confidence       INTEGER NOT NULL CHECK (confidence >= 1 AND confidence <= 5),
    status           TEXT    NOT NULL CHECK (status IN (
                         'proposed','accepted','rejected','superseded',
                         'active','archived','unresolved','deprecated'
                     )),
    tags_json        TEXT    NOT NULL DEFAULT '[]',
    related_ids_json TEXT    NOT NULL DEFAULT '[]',
    created_by       TEXT    NOT NULL CHECK (created_by != ''),
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS memory_revisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id      INTEGER NOT NULL,
    old_value_json TEXT    NOT NULL,
    new_value_json TEXT    NOT NULL,
    reason         TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    created_by     TEXT    NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memory_events(id)
);

CREATE TABLE IF NOT EXISTS memory_links (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id          INTEGER NOT NULL,
    target_id          INTEGER NOT NULL,
    relationship       TEXT    NOT NULL CHECK (relationship IN (
                           'supports','contradicts','supersedes','refines',
                           'derived_from','related_to','blocks','depends_on'
                       )),
    created_at         TEXT    NOT NULL,
    created_by         TEXT,
    reason             TEXT,
    link_confidence    INTEGER,
    link_metadata_json TEXT,
    status             TEXT    NOT NULL DEFAULT 'active',
    retracted_at       TEXT,
    retracted_reason   TEXT,
    retracted_by       TEXT,
    FOREIGN KEY (source_id) REFERENCES memory_events(id),
    FOREIGN KEY (target_id) REFERENCES memory_events(id),
    UNIQUE (source_id, target_id, relationship)
);
-- idx_links_status and idx_links_contradicts are created by _migrate_to_v8() in service.py.

CREATE INDEX IF NOT EXISTS idx_events_type   ON memory_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_status ON memory_events(status);
CREATE INDEX IF NOT EXISTS idx_revisions_mid ON memory_revisions(memory_id);
CREATE INDEX IF NOT EXISTS idx_links_src     ON memory_links(source_id);
CREATE INDEX IF NOT EXISTS idx_links_tgt     ON memory_links(target_id);

CREATE TABLE IF NOT EXISTS retrieval_log (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    query_hash               TEXT    NOT NULL,
    session_id               TEXT,
    query_json               TEXT    NOT NULL,
    scoring_version          TEXT    NOT NULL,
    scoring_params_json      TEXT    NOT NULL,
    result_event_ids_json    TEXT    NOT NULL,
    result_count             INTEGER NOT NULL,
    executed_at              TEXT    NOT NULL,
    actor                    TEXT    NOT NULL,
    status                   TEXT    NOT NULL DEFAULT 'active',
    semantic_mode            TEXT    NOT NULL DEFAULT 'none',
    semantic_provenance_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_retrieval_log_query_hash      ON retrieval_log(query_hash);
CREATE INDEX IF NOT EXISTS idx_retrieval_log_scoring_version ON retrieval_log(scoring_version);
CREATE INDEX IF NOT EXISTS idx_retrieval_log_session_id      ON retrieval_log(session_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_log_executed_at     ON retrieval_log(executed_at);
-- idx_retrieval_log_status is created by _migrate_to_v3() in service.py, not here.
-- This avoids the CREATE INDEX failing on v2 DBs where status does not exist yet
-- when executescript() runs. The migration adds the column first, then the index.

CREATE TABLE IF NOT EXISTS event_embeddings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_event_id     INTEGER NOT NULL,
    content_hash        TEXT    NOT NULL,
    vector_json         TEXT    NOT NULL,
    dimensions          INTEGER NOT NULL,
    model_name          TEXT    NOT NULL,
    model_version       TEXT    NOT NULL,
    model_digest        TEXT,
    provider_name       TEXT    NOT NULL,
    adapter_name        TEXT    NOT NULL,
    adapter_version     TEXT    NOT NULL,
    producer_version    TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'candidate',
    generated_at        TEXT    NOT NULL,
    invalidated_at      TEXT,
    invalidated_reason  TEXT,
    provenance_json     TEXT    NOT NULL,
    FOREIGN KEY (memory_event_id) REFERENCES memory_events(id),
    UNIQUE (memory_event_id, content_hash, producer_version)
);

-- Governance: event_embeddings is a local derived artifact.
-- It is excluded from continuity bundles by governance policy.
-- Future portability can be considered explicitly, not silently.
CREATE INDEX IF NOT EXISTS idx_embeddings_event_id         ON event_embeddings(memory_event_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_content_hash     ON event_embeddings(content_hash);
CREATE INDEX IF NOT EXISTS idx_embeddings_status           ON event_embeddings(status);
CREATE INDEX IF NOT EXISTS idx_embeddings_producer_version ON event_embeddings(producer_version);

CREATE TABLE IF NOT EXISTS embedding_model_pins (
    id                               INTEGER PRIMARY KEY AUTOINCREMENT,
    pin_scope                        TEXT    NOT NULL DEFAULT 'global',
    adapter_name                     TEXT    NOT NULL,
    adapter_version                  TEXT    NOT NULL,
    model_name                       TEXT    NOT NULL,
    model_digest                     TEXT,
    dimensions                       INTEGER NOT NULL,
    embedding_visible_fields_version TEXT    NOT NULL DEFAULT '1',
    pin_identity                     TEXT    NOT NULL,
    provider_name                    TEXT    NOT NULL,
    status                           TEXT    NOT NULL DEFAULT 'active',
    pinned_at                        TEXT    NOT NULL,
    pinned_by                        TEXT    NOT NULL,
    superseded_at                    TEXT,
    superseded_reason                TEXT,
    notes                            TEXT
);
-- idx_pins_scope_status is created by _migrate_to_v5() in service.py, not here.
-- This avoids CREATE INDEX failing on v4 DBs before the migration runs.
-- idx_pins_identity and idx_pins_pinned_at follow the same pattern.

CREATE TABLE IF NOT EXISTS context_assembly_log (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    assembly_hash                TEXT    NOT NULL UNIQUE,
    session_id                   TEXT    NOT NULL,
    assembly_version             TEXT    NOT NULL,
    assembled_at                 TEXT    NOT NULL,
    db_path                      TEXT    NOT NULL,
    policy_json                  TEXT    NOT NULL,
    query_vector_hash            TEXT,
    query_vector_provenance_json TEXT,
    entries_accepted             INTEGER NOT NULL,
    entries_rejected_budget      INTEGER NOT NULL DEFAULT 0,
    entries_rejected_filter      INTEGER NOT NULL DEFAULT 0,
    char_budget_used             INTEGER NOT NULL,
    char_budget_limit            INTEGER NOT NULL,
    compression_mode             TEXT    NOT NULL DEFAULT 'none',
    assembly_snapshot_json       TEXT    NOT NULL,
    status                       TEXT    NOT NULL DEFAULT 'active',
    superseded_at                TEXT,
    superseded_reason            TEXT
);
-- Indices for context_assembly_log are created by _migrate_to_v7() in service.py.
-- This follows the same pattern as embedding_model_pins / retrieval_log status index.

CREATE TABLE IF NOT EXISTS confidence_revisions (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_event_id             INTEGER NOT NULL,
    confidence_before           INTEGER NOT NULL,
    confidence_after            INTEGER NOT NULL CHECK (confidence_after >= 1 AND confidence_after <= 5),
    revised_by                  TEXT    NOT NULL,
    reason                      TEXT    NOT NULL,
    revision_type               TEXT    NOT NULL DEFAULT 'operator',
    status                      TEXT    NOT NULL DEFAULT 'active',
    contradiction_link_ids_json TEXT,
    evidence                    TEXT,
    provenance_json             TEXT,
    created_at                  TEXT    NOT NULL,
    superseded_at               TEXT,
    rejected_at                 TEXT,
    rejected_by                 TEXT,
    rejected_reason             TEXT,
    FOREIGN KEY (memory_event_id) REFERENCES memory_events(id)
);
-- Indices for confidence_revisions are created by _migrate_to_v9() in service.py.

CREATE TABLE IF NOT EXISTS cognition_session (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key             TEXT    NOT NULL,
    status                  TEXT    NOT NULL DEFAULT 'active',
    started_at              TEXT    NOT NULL,
    closed_at               TEXT,
    closed_reason           TEXT,
    initial_assembly_id     INTEGER,
    latest_assembly_id      INTEGER,
    assembly_count          INTEGER NOT NULL DEFAULT 0,
    db_path                 TEXT    NOT NULL,
    policy_fingerprint_json TEXT    NOT NULL,
    metadata_json           TEXT
);

CREATE TABLE IF NOT EXISTS assembly_transition_log (
    id                                      INTEGER PRIMARY KEY AUTOINCREMENT,
    cognition_session_id                    INTEGER NOT NULL,
    sequence_index                          INTEGER NOT NULL,
    from_assembly_id                        INTEGER,
    to_assembly_id                          INTEGER NOT NULL,
    transition_type                         TEXT    NOT NULL,
    transition_reason                       TEXT    NOT NULL,
    triggered_by                            TEXT    NOT NULL,
    transitioned_at                         TEXT    NOT NULL,
    triggering_retrieval_ids_json           TEXT,
    triggering_confidence_revision_ids_json TEXT,
    triggering_contradiction_link_ids_json  TEXT,
    provenance_json                         TEXT,
    UNIQUE (cognition_session_id, sequence_index),
    FOREIGN KEY (cognition_session_id) REFERENCES cognition_session(id),
    FOREIGN KEY (to_assembly_id) REFERENCES context_assembly_log(id)
);
-- Indices for cognition_session and assembly_transition_log are created by
-- _migrate_to_v10() in service.py.
