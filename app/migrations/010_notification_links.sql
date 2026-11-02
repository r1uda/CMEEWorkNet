CREATE TABLE notification_links (
 message_id TEXT PRIMARY KEY REFERENCES messages(id),
 event_key TEXT NOT NULL UNIQUE,
 target_url TEXT NOT NULL
);
