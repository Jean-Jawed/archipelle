-- Historique des conversations (CDC §12). Le contenu brut des résultats d'outils
-- n'est jamais archivé : les éléments « tool_results » ne portent que des marqueurs.

CREATE TABLE conversations (
    id              TEXT PRIMARY KEY,
    title           TEXT    NOT NULL DEFAULT '',
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    provider_id     TEXT    NOT NULL,
    provider_locked INTEGER NOT NULL DEFAULT 0,
    model           TEXT    NOT NULL,
    mode            TEXT    NOT NULL DEFAULT 'quick' CHECK (mode IN ('quick', 'explore')),
    workdir         TEXT,
    scope_rel       TEXT,
    tree_text       TEXT,
    tree_digest     TEXT
);
CREATE INDEX conversations_updated ON conversations (updated_at DESC);

CREATE TABLE turns (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT    NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    run_id          TEXT    NOT NULL,
    mode            TEXT    NOT NULL CHECK (mode IN ('quick', 'explore')),
    status          TEXT    NOT NULL
        CHECK (status IN ('in_progress', 'complete', 'partial', 'interrupted')),
    workdir         TEXT,
    started_at      REAL    NOT NULL,
    ended_at        REAL,
    restore_mode    TEXT,
    restore_model   TEXT,
    UNIQUE (conversation_id, seq)
);
CREATE INDEX turns_status ON turns (status);

CREATE TABLE items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id     TEXT    NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN (
                    'user', 'system_notice', 'tree', 'assistant', 'tool_results', 'interruption')),
    payload     TEXT    NOT NULL,
    provider    TEXT,
    model       TEXT,
    created_at  REAL    NOT NULL,
    UNIQUE (turn_id, seq)
);

CREATE TABLE consulted_files (
    conversation_id TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    turn_id         TEXT NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    workdir         TEXT NOT NULL,
    rel_path        TEXT NOT NULL,
    PRIMARY KEY (turn_id, workdir, rel_path)
);
CREATE INDEX consulted_by_conversation ON consulted_files (conversation_id, workdir);

CREATE TABLE sources (
    item_id     INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL,
    workdir     TEXT,
    rel_path    TEXT    NOT NULL,
    verified    INTEGER NOT NULL,
    PRIMARY KEY (item_id, ord)
);

CREATE TABLE ignored_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id     TEXT NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    rel_path    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX ignored_by_turn ON ignored_files (turn_id);

CREATE TABLE turn_steps (
    turn_id     TEXT    NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    payload     TEXT    NOT NULL,
    PRIMARY KEY (turn_id, seq)
);

-- Recherche dans le contenu des conversations (barre latérale), insensible aux accents.
CREATE VIRTUAL TABLE items_fts USING fts5 (
    text,
    conversation_id UNINDEXED,
    item_id UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
