CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL CHECK(title != ''),
    description   TEXT,
    task_type     TEXT    NOT NULL,
    state         TEXT    NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 3,
    actor         TEXT    NOT NULL CHECK(actor != ''),
    tags_json     TEXT    NOT NULL DEFAULT '[]',
    metadata_json TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS task_lineage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             INTEGER NOT NULL,
    old_state           TEXT,
    new_state           TEXT    NOT NULL,
    reason              TEXT    NOT NULL,
    actor               TEXT    NOT NULL,
    dependency_snapshot TEXT    NOT NULL DEFAULT '[]',
    metadata_json       TEXT    NOT NULL DEFAULT '{}',
    created_at          TEXT    NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL,
    depends_on_id   INTEGER NOT NULL,
    dependency_type TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    FOREIGN KEY (task_id)       REFERENCES tasks(id),
    FOREIGN KEY (depends_on_id) REFERENCES tasks(id),
    UNIQUE (task_id, depends_on_id)
);
