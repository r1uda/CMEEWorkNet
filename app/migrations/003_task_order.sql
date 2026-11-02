CREATE TABLE task_positions (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),
 position INTEGER NOT NULL CHECK(position>=0)
);
