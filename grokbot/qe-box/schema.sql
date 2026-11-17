-- Box-only state. Closed values are parsed as enums and bound by qe_box.py.
PRAGMA journal_mode = DELETE;
CREATE TABLE IF NOT EXISTS floor_ledger (
 batch TEXT PRIMARY KEY, max_floor INTEGER NOT NULL, max_rev INTEGER NOT NULL, owned INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS floor_record (
 batch TEXT PRIMARY KEY, task TEXT NOT NULL, callback_bot TEXT NOT NULL, identity_json TEXT NOT NULL,
 latest_rev INTEGER NOT NULL, floor INTEGER NOT NULL, owned INTEGER NOT NULL, state TEXT NOT NULL,
 blocked_cause TEXT, correction_floor INTEGER, correction_notified_at TEXT, progress_seq INTEGER NOT NULL,
 send_failures INTEGER NOT NULL, last_activity_at TEXT NOT NULL, closed_at TEXT, tombstoned_at TEXT,
 local_resolution INTEGER, local_level INTEGER, kept_resolution INTEGER, escalated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS revision (
 batch TEXT NOT NULL, rev INTEGER NOT NULL, content_json TEXT NOT NULL, content_digest TEXT NOT NULL,
 box_floor INTEGER NOT NULL, hits_json TEXT NOT NULL, guard_version TEXT NOT NULL, registered_at TEXT NOT NULL,
 PRIMARY KEY(batch, rev)
);
CREATE TABLE IF NOT EXISTS progress (
 batch TEXT NOT NULL, rev INTEGER NOT NULL, seq INTEGER NOT NULL, json TEXT NOT NULL, applied_at TEXT NOT NULL,
 PRIMARY KEY(batch, seq)
);
CREATE TABLE IF NOT EXISTS queued_registration (
 n INTEGER PRIMARY KEY, batch TEXT NOT NULL, kind TEXT NOT NULL, rev INTEGER NOT NULL, seq INTEGER NOT NULL,
 content_digest TEXT NOT NULL, json TEXT NOT NULL, queued_at TEXT NOT NULL,
 UNIQUE(batch, kind, rev, seq, content_digest)
);
CREATE TABLE IF NOT EXISTS lease (
 batch TEXT PRIMARY KEY, lease_id TEXT UNIQUE NOT NULL, delivery_id TEXT NOT NULL, plan_id TEXT NOT NULL,
 batch_rev INTEGER NOT NULL, floor INTEGER NOT NULL, issued_at TEXT NOT NULL, dead_at TEXT NOT NULL,
 expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision (
 batch TEXT NOT NULL, resolution_rev INTEGER NOT NULL, batch_rev INTEGER NOT NULL, level INTEGER NOT NULL,
 kind TEXT NOT NULL, supersedes INTEGER, answers_json TEXT NOT NULL, clearances_json TEXT NOT NULL,
 decided_by TEXT NOT NULL, card_gen INTEGER, card_ref TEXT, at TEXT NOT NULL,
 PRIMARY KEY(batch, resolution_rev)
);
CREATE TABLE IF NOT EXISTS card_generation (
 task TEXT NOT NULL, callback_bot TEXT NOT NULL, gen INTEGER NOT NULL, purpose TEXT NOT NULL,
 pairs_json TEXT NOT NULL, state TEXT NOT NULL, ref TEXT, reconciles INTEGER, close_requested INTEGER NOT NULL,
 at TEXT NOT NULL, reminded INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(task, callback_bot, gen)
);
CREATE TABLE IF NOT EXISTS handoff (
 id TEXT PRIMARY KEY, task TEXT NOT NULL, callback_bot TEXT NOT NULL, purpose TEXT NOT NULL,
 batch_revs_json TEXT NOT NULL, sent_count INTEGER NOT NULL, last_sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery (
 id TEXT PRIMARY KEY, batch TEXT NOT NULL, resolution_rev INTEGER NOT NULL, purpose TEXT NOT NULL,
 target_actor TEXT NOT NULL, child_actor TEXT, text TEXT NOT NULL, text_digest TEXT NOT NULL,
 body_digest TEXT NOT NULL, ack_token TEXT NOT NULL, route_set TEXT NOT NULL, state TEXT NOT NULL,
 suppress_cause TEXT, paused_by TEXT, attempt INTEGER NOT NULL, resend_allowed INTEGER NOT NULL,
 authorized_rev INTEGER NOT NULL, authorized_floor INTEGER NOT NULL, failed_sends INTEGER NOT NULL,
 late_result_json TEXT, delivered_at TEXT, delivered_via TEXT, updated_at TEXT NOT NULL,
 UNIQUE(batch, resolution_rev, purpose, target_actor)
);
CREATE TABLE IF NOT EXISTS plan (
 id TEXT PRIMARY KEY, action TEXT NOT NULL, lease_id TEXT, delivery_id TEXT, attempt INTEGER,
 body_json TEXT NOT NULL, plan_digest TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT,
 issued_by TEXT NOT NULL, issued_at TEXT NOT NULL, final_at TEXT, late_result_json TEXT
);
CREATE TABLE IF NOT EXISTS takeover_seen (
 takeover_id TEXT PRIMARY KEY, batch TEXT NOT NULL, origin TEXT NOT NULL, cleared INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tombstone (
 batch TEXT PRIMARY KEY, task TEXT NOT NULL, final_state TEXT NOT NULL, last_rev INTEGER NOT NULL,
 record_digest TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS box_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, batch TEXT, detail TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_outbound_seen (id TEXT PRIMARY KEY, batch TEXT NOT NULL, state TEXT NOT NULL);
-- Storage also enforces the write-once evidence contract against future writers.
CREATE TRIGGER IF NOT EXISTS immutable_delivery_evidence
BEFORE UPDATE OF delivered_at, delivered_via ON delivery
WHEN OLD.delivered_at IS NOT NULL AND
 (NEW.delivered_at IS NOT OLD.delivered_at OR NEW.delivered_via IS NOT OLD.delivered_via)
BEGIN SELECT RAISE(ABORT, 'delivery evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS permanent_floor_ledger BEFORE DELETE ON floor_ledger
BEGIN SELECT RAISE(ABORT, 'floor ledger is permanent'); END;
