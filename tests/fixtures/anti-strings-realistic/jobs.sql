CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0
);

INSERT INTO jobs (id, status) VALUES (1, 'queued');

SELECT id FROM jobs WHERE status = 'queued' AND attempts < 3;

UPDATE jobs
SET status = 'running', attempts = attempts + 1
WHERE status = 'queued';
