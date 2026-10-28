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

-- Signed checkpoints (D30). The note is the full C2SP signed note and the only column the
-- verifier trusts; tree_size and root_hash are copies for queries. Also written to a
-- write-once directory by DirectorySink.
CREATE TABLE IF NOT EXISTS checkpoint (
    stream     text   NOT NULL,
    tree_size  bigint NOT NULL CHECK (tree_size >= 0),
    root_hash  text   NOT NULL,
    note       text   NOT NULL,
    PRIMARY KEY (stream, tree_size)
);

-- Witness cosignatures (D39): one C2SP cosignature/v1 signature line per witness per
-- checkpoint. The line is the only column the verifier trusts; it is checked under the
-- witness's key before it is stored and again before it is served or exported.
CREATE TABLE IF NOT EXISTS cosignature (
    stream     text   NOT NULL,
    tree_size  bigint NOT NULL,
    witness    text   NOT NULL,
    line       text   NOT NULL,
    PRIMARY KEY (stream, tree_size, witness),
    FOREIGN KEY (stream, tree_size) REFERENCES checkpoint (stream, tree_size)
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

CREATE OR REPLACE TRIGGER checkpoint_append_only
    BEFORE UPDATE OR DELETE ON checkpoint
    FOR EACH ROW EXECUTE FUNCTION pl_reject_change();
CREATE OR REPLACE TRIGGER checkpoint_no_truncate
    BEFORE TRUNCATE ON checkpoint
    FOR EACH STATEMENT EXECUTE FUNCTION pl_reject_change();

CREATE OR REPLACE TRIGGER cosignature_append_only
    BEFORE UPDATE OR DELETE ON cosignature
    FOR EACH ROW EXECUTE FUNCTION pl_reject_change();
CREATE OR REPLACE TRIGGER cosignature_no_truncate
    BEFORE TRUNCATE ON cosignature
    FOR EACH STATEMENT EXECUTE FUNCTION pl_reject_change();

REVOKE ALL ON ledger_record, receipt, checkpoint, cosignature FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pl_app') THEN
        GRANT SELECT, INSERT ON ledger_record, receipt, checkpoint, cosignature TO pl_app;
    END IF;
END;
$$;
