CREATE TABLE workflow_mail_links (
 outbox_id TEXT PRIMARY KEY REFERENCES outbox(id),
 entity TEXT NOT NULL CHECK(entity IN ('swap','handoff','public')),
 entity_id TEXT NOT NULL,
 event TEXT NOT NULL
);
