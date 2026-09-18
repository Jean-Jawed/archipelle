-- Index du cache d'extraction (CDC §4bis). Écrit uniquement par le processus principal.

CREATE TABLE documents (
    doc_key       TEXT PRIMARY KEY,           -- hachage du chemin absolu normalisé
    abs_path      TEXT    NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    size          INTEGER NOT NULL,
    page_count    INTEGER,                    -- NULL tant que le document n'a pas été inspecté
    notes         TEXT,                       -- JSON : avertissements d'extraction (xlsx sans valeurs…)
    bytes_on_disk INTEGER NOT NULL DEFAULT 0,
    last_used     REAL    NOT NULL
);
CREATE INDEX documents_lru ON documents (last_used);

CREATE TABLE pages (
    doc_key   TEXT    NOT NULL REFERENCES documents (doc_key) ON DELETE CASCADE,
    page      INTEGER NOT NULL,
    method    TEXT    NOT NULL CHECK (method IN ('native', 'ocr', 'pending')),
    chars     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (doc_key, page)
);
