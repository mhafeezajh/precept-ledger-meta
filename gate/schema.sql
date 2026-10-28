-- Gate app state (not ledger state): approval requests. Idempotent; `make db-schema` applies it
-- after ledger/schema.sql. What proves a verdict is its gate.verdict ledger record; this table
-- is the guard that lets each escalation close exactly once.

CREATE TABLE IF NOT EXISTS escalation (
    id                 text PRIMARY KEY,
    stream             text NOT NULL,  -- the ledger stream its records go to; one Gate per stream
    decision_record_id text NOT NULL UNIQUE,
    agent              text NOT NULL,
    tool               text NOT NULL,
    action             text NOT NULL,
    approver_roles     text[] NOT NULL,
    opened_at          timestamptz NOT NULL,
    expires_at         timestamptz NOT NULL,
    state              text NOT NULL DEFAULT 'open'
                       CHECK (state IN ('open', 'approved', 'rejected', 'expired')),
    approver           text,
    verdict_record_id  text,
    decided_at         timestamptz,
    CHECK ((state = 'open') = (decided_at IS NULL)),
    CHECK (state <> 'approved' OR (approver IS NOT NULL AND verdict_record_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS escalation_open_by_expiry ON escalation (stream, expires_at) WHERE state = 'open';

-- The app may close an open escalation and nothing else: no reopening, no edits to what was
-- asked, no deletes. The demo admin connection is not exempt.
CREATE OR REPLACE FUNCTION pl_escalation_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'escalation rows are never deleted';
    END IF;
    IF OLD.state <> 'open' THEN
        RAISE EXCEPTION 'escalation % is already %', OLD.id, OLD.state;
    END IF;
    IF (NEW.id, NEW.stream, NEW.decision_record_id, NEW.agent, NEW.tool, NEW.action,
        NEW.approver_roles, NEW.opened_at, NEW.expires_at)
       IS DISTINCT FROM
       (OLD.id, OLD.stream, OLD.decision_record_id, OLD.agent, OLD.tool, OLD.action,
        OLD.approver_roles, OLD.opened_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'only the outcome of escalation % may change', OLD.id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER escalation_guard
    BEFORE UPDATE OR DELETE ON escalation
    FOR EACH ROW EXECUTE FUNCTION pl_escalation_guard();
CREATE OR REPLACE TRIGGER escalation_no_truncate
    BEFORE TRUNCATE ON escalation
    FOR EACH STATEMENT EXECUTE FUNCTION pl_reject_change();

REVOKE ALL ON escalation FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pl_app') THEN
        GRANT SELECT, INSERT, UPDATE ON escalation TO pl_app;
    END IF;
END;
$$;
