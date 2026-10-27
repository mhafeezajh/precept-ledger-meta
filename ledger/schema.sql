-- Ledger tables (D3, D21). Insert-only: triggers reject UPDATE, DELETE and TRUNCATE for every
-- role, and the app role is granted only INSERT and SELECT. Records are stored as typed
-- columns plus body jsonb and rebuilt from those columns for verification, so a direct edit
-- of any column changes what the verifier sees.

CREATE TABLE IF NOT EXISTS ledger_record (
    stream          text    NOT NULL,
    seq             bigint  NOT NULL CHECK (seq >= 1),
    record_id       text    NOT NULL UNIQUE,
    v               integer NOT NULL,
    ts              text    NOT NULL,  -- kept as text: the exact signed string
    type            text    NOT NULL,
    producer_id     text    NOT NULL,
    producer_alg    text    NOT NULL,
    producer_key_id text    NOT NULL,
    producer_sig    text    NOT NULL,
    body            jsonb   NOT NULL,
    prev_hash       text    NOT NULL,
    chain_hash      text    NOT NULL,
    leaf_hash       text    NOT NULL,
    PRIMARY KEY (stream, seq)
);

CREATE INDEX IF NOT EXISTS ledger_record_type_ts ON ledger_record (stream, type, ts);

CREATE TABLE IF NOT EXISTS receipt (
    record_id   text    PRIMARY KEY REFERENCES ledger_record (record_id),
    v           integer NOT NULL,
    alg         text    NOT NULL,
    stream      text    NOT NULL,
    seq         bigint  NOT NULL,
    leaf_hash   text    NOT NULL,
    chain_hash  text    NOT NULL,
    received_at text    NOT NULL,
    key_id      text    NOT NULL,
    sig         text    NOT NULL,
    UNIQUE (stream, seq)
);

CREATE OR REPLACE FUNCTION pl_reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ledger tables are append-only: % on % rejected', TG_OP, TG_TABLE_NAME;
END;
$$;

CREATE OR REPLACE TRIGGER ledger_record_append_only
    BEFORE UPDATE OR DELETE ON ledger_record
    FOR EACH ROW EXECUTE FUNCTION pl_reject_change();
CREATE OR REPLACE TRIGGER ledger_record_no_truncate
    BEFORE TRUNCATE ON ledger_record
    FOR EACH STATEMENT EXECUTE FUNCTION pl_reject_change();
CREATE OR REPLACE TRIGGER receipt_append_only
    BEFORE UPDATE OR DELETE ON receipt
    FOR EACH ROW EXECUTE FUNCTION pl_reject_change();
CREATE OR REPLACE TRIGGER receipt_no_truncate
    BEFORE TRUNCATE ON receipt
    FOR EACH STATEMENT EXECUTE FUNCTION pl_reject_change();

REVOKE ALL ON ledger_record, receipt FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pl_app') THEN
        GRANT SELECT, INSERT ON ledger_record, receipt TO pl_app;
    END IF;
END;
$$;
