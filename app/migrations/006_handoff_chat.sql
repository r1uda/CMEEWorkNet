CREATE TABLE direct_tasks (
 id TEXT PRIMARY KEY, text TEXT NOT NULL, creator_id TEXT NOT NULL REFERENCES members(id),
 assignee_id TEXT NOT NULL REFERENCES members(id), status TEXT NOT NULL CHECK(status IN ('pending','done')),
 created_at TEXT NOT NULL
);
CREATE TABLE handoffs_next (
 id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), sender_id TEXT NOT NULL REFERENCES members(id),
 recipient_id TEXT NOT NULL REFERENCES members(id), reason TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected','stale')), created_at TEXT NOT NULL, decided_at TEXT,
 direct_task_id TEXT REFERENCES direct_tasks(id),
 CHECK((task_id IS NOT NULL) != (direct_task_id IS NOT NULL))
);
INSERT INTO handoffs_next(id,task_id,sender_id,recipient_id,reason,state,created_at,decided_at)
 SELECT id,task_id,sender_id,recipient_id,reason,state,created_at,decided_at FROM handoffs;
DROP TABLE handoffs;
ALTER TABLE handoffs_next RENAME TO handoffs;
CREATE UNIQUE INDEX pending_handoff ON handoffs(task_id) WHERE state='pending';
CREATE UNIQUE INDEX pending_direct_handoff ON handoffs(direct_task_id) WHERE state='pending';
CREATE TABLE handoff_comments (
 id TEXT PRIMARY KEY, handoff_id TEXT NOT NULL REFERENCES handoffs(id), author_id TEXT NOT NULL REFERENCES members(id),
 text TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX handoff_comments_thread ON handoff_comments(handoff_id,created_at);
CREATE TABLE task_followups (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), member_id TEXT NOT NULL REFERENCES members(id), created_at TEXT NOT NULL
);
CREATE TABLE direct_progress (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES direct_tasks(id), author_id TEXT NOT NULL REFERENCES members(id),
 text TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','done')), created_at TEXT NOT NULL
);
