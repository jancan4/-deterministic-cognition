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
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER NOT NULL,
    target_id    INTEGER NOT NULL,
    relationship TEXT    NOT NULL CHECK (relationship IN (
                     'supports','contradicts','supersedes','refines',
                     'derived_from','related_to','blocks','depends_on'
                 )),
    created_at   TEXT    NOT NULL,
    FOREIGN KEY (source_id) REFERENCES memory_events(id),
    FOREIGN KEY (target_id) REFERENCES memory_events(id),
    UNIQUE (source_id, target_id, relationship)
);

CREATE INDEX IF NOT EXISTS idx_events_type   ON memory_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_status ON memory_events(status);
CREATE INDEX IF NOT EXISTS idx_revisions_mid ON memory_revisions(memory_id);
CREATE INDEX IF NOT EXISTS idx_links_src     ON memory_links(source_id);
CREATE INDEX IF NOT EXISTS idx_links_tgt     ON memory_links(target_id);
