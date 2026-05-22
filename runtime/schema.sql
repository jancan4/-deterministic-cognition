CREATE TABLE IF NOT EXISTS runtimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK(name != ''),
    state TEXT NOT NULL,
    orchestration_db TEXT NOT NULL CHECK(orchestration_db != ''),
    config_json TEXT NOT NULL DEFAULT '{}',
    current_iteration INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS runtime_lineage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id INTEGER NOT NULL,
    old_state TEXT,
    new_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    iteration INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (runtime_id) REFERENCES runtimes(id)
);

CREATE TABLE IF NOT EXISTS runtime_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id INTEGER NOT NULL,
    iteration INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (runtime_id) REFERENCES runtimes(id)
);
