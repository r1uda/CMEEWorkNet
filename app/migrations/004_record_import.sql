CREATE TABLE record_import_previews (
 id TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES members(id),
 payload TEXT NOT NULL, baseline TEXT NOT NULL, created_at TEXT NOT NULL, applied_at TEXT
);
CREATE TABLE record_import_sources (
 fingerprint TEXT PRIMARY KEY, registration_id TEXT NOT NULL REFERENCES registrations(id),
 preview_id TEXT NOT NULL REFERENCES record_import_previews(id), source_row TEXT NOT NULL
);
