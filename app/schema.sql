PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES (1);
CREATE TABLE IF NOT EXISTS members (
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, email TEXT UNIQUE,
 caps TEXT NOT NULL DEFAULT '["can_register"]', active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES members(id),
 expires_at TEXT NOT NULL, last_seen TEXT NOT NULL, csrf TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
 email TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
 sent_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shifts (
 id TEXT PRIMARY KEY, date TEXT NOT NULL, slot INTEGER NOT NULL CHECK(slot BETWEEN 0 AND 3),
 original_member_id TEXT NOT NULL REFERENCES members(id), member_id TEXT NOT NULL REFERENCES members(id),
 version INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1,
 UNIQUE(date,slot)
);
CREATE TABLE IF NOT EXISTS registrations (
 id TEXT PRIMARY KEY, shift_id TEXT UNIQUE REFERENCES shifts(id), author_id TEXT NOT NULL REFERENCES members(id),
 date TEXT NOT NULL, slot INTEGER, state TEXT NOT NULL CHECK(state IN ('draft','submitted')),
 notes TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, submitted_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, registration_id TEXT NOT NULL REFERENCES registrations(id), text TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','done')), active INTEGER NOT NULL DEFAULT 1,
 assignee_id TEXT NOT NULL REFERENCES members(id)
);
CREATE TABLE IF NOT EXISTS record_history (
 id TEXT PRIMARY KEY, registration_id TEXT NOT NULL REFERENCES registrations(id), actor_id TEXT NOT NULL REFERENCES members(id),
 snapshot TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS swaps (
 id TEXT PRIMARY KEY, shift_id TEXT NOT NULL REFERENCES shifts(id), target_shift_id TEXT REFERENCES shifts(id),
 requester_id TEXT NOT NULL REFERENCES members(id), recipient_id TEXT NOT NULL REFERENCES members(id),
 shift_version INTEGER NOT NULL, target_version INTEGER, reason TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected','stale')), created_at TEXT NOT NULL, decided_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS pending_swap ON swaps(shift_id) WHERE state='pending';
CREATE TABLE IF NOT EXISTS handoffs (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), sender_id TEXT NOT NULL REFERENCES members(id),
 recipient_id TEXT NOT NULL REFERENCES members(id), reason TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected','stale')), created_at TEXT NOT NULL, decided_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS pending_handoff ON handoffs(task_id) WHERE state='pending';
CREATE TABLE IF NOT EXISTS progress (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), author_id TEXT NOT NULL REFERENCES members(id),
 text TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','done')), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, sender_id TEXT NOT NULL REFERENCES members(id), body TEXT NOT NULL,
 registration_id TEXT REFERENCES registrations(id), reply_to TEXT REFERENCES messages(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_recipients (
 message_id TEXT NOT NULL REFERENCES messages(id), member_id TEXT NOT NULL REFERENCES members(id),
 read_at TEXT, confirmed_at TEXT, PRIMARY KEY(message_id,member_id)
);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'reminder',
 shift_id TEXT REFERENCES shifts(id), shift_version INTEGER, recipient_id TEXT NOT NULL REFERENCES members(id),
 subject TEXT NOT NULL, body TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','uncertain','canceled')),
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, created_at TEXT NOT NULL,
 sent_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS import_previews (
 id TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES members(id), payload TEXT NOT NULL,
 fingerprint TEXT NOT NULL, baseline TEXT NOT NULL, created_at TEXT NOT NULL, applied_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
 id TEXT PRIMARY KEY, actor_id TEXT, action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_usage (day TEXT NOT NULL, member_id TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(day,member_id));
CREATE TABLE IF NOT EXISTS ai_proposals (
 id TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES members(id), action TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL, confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS shifts_due ON shifts(date,active);
CREATE INDEX IF NOT EXISTS registrations_author ON registrations(author_id,date);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status,next_attempt_at);
