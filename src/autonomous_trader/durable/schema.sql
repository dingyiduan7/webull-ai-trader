CREATE TABLE IF NOT EXISTS schema_version (version integer PRIMARY KEY CHECK (version = 1));
INSERT INTO schema_version VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS accounts (
    account_id text PRIMARY KEY,
    environment text NOT NULL DEFAULT 'OFFLINE' CHECK (environment = 'OFFLINE'),
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    state jsonb NOT NULL DEFAULT '{}',
    state_hash text NOT NULL DEFAULT '',
    head_hash text NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS journal (
    account_id text NOT NULL REFERENCES accounts(account_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_id text NOT NULL,
    schema_version integer NOT NULL CHECK (schema_version = 1),
    kind text NOT NULL,
    correlation_id text NOT NULL,
    causation_id text,
    source_id text NOT NULL,
    logical_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    request_hash text NOT NULL,
    previous_hash text NOT NULL,
    event_hash text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY(account_id, sequence),
    UNIQUE(account_id, event_id)
);
CREATE OR REPLACE FUNCTION reject_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'append-only journal: mutation prohibited'; END; $$;
DROP TRIGGER IF EXISTS journal_no_mutation ON journal;
CREATE TRIGGER journal_no_mutation BEFORE UPDATE OR DELETE OR TRUNCATE ON journal
FOR EACH STATEMENT EXECUTE FUNCTION reject_history_mutation();

-- Disposable relational projections, replaced atomically with the account checkpoint.
CREATE TABLE IF NOT EXISTS cash_balances (
    account_id text PRIMARY KEY REFERENCES accounts(account_id),
    settled_usd numeric NOT NULL, reserved_usd numeric NOT NULL CHECK(reserved_usd >= 0),
    unsettled_usd numeric NOT NULL, realized_gross_usd numeric NOT NULL,
    fees_usd numeric NOT NULL, realized_net_usd numeric NOT NULL,
    unrealized_usd numeric NOT NULL, liquidation_equity_usd numeric NOT NULL
);
CREATE TABLE IF NOT EXISTS position_lots (
    account_id text REFERENCES accounts(account_id), lot_id text,
    instrument_id text NOT NULL, episode_id text NOT NULL,
    quantity numeric NOT NULL CHECK(quantity >= 0), entry_price_usd numeric NOT NULL,
    data jsonb NOT NULL, PRIMARY KEY(account_id, lot_id)
);
CREATE TABLE IF NOT EXISTS positions (
    account_id text REFERENCES accounts(account_id), instrument_id text,
    quantity numeric NOT NULL CHECK(quantity >= 0), data jsonb NOT NULL,
    PRIMARY KEY(account_id, instrument_id)
);
CREATE TABLE IF NOT EXISTS settlement_lots (
    account_id text REFERENCES accounts(account_id), execution_id text,
    amount_usd numeric NOT NULL CHECK(amount_usd >= 0), confirmed_usd numeric NOT NULL,
    settlement_date date NOT NULL, data jsonb NOT NULL,
    PRIMARY KEY(account_id, execution_id)
);
CREATE TABLE IF NOT EXISTS reservations (
    account_id text REFERENCES accounts(account_id), intent_id text,
    cash_usd numeric NOT NULL CHECK(cash_usd >= 0), shares numeric NOT NULL CHECK(shares >= 0),
    exposure_usd numeric NOT NULL CHECK(exposure_usd >= 0), risk_usd numeric NOT NULL CHECK(risk_usd >= 0),
    data jsonb NOT NULL, PRIMARY KEY(account_id, intent_id)
);
CREATE TABLE IF NOT EXISTS safety_latches (
    account_id text REFERENCES accounts(account_id), name text, active boolean NOT NULL,
    PRIMARY KEY(account_id, name)
);
CREATE TABLE IF NOT EXISTS segments (
    account_id text REFERENCES accounts(account_id), segment_id text,
    reference_usd numeric NOT NULL, high_water_usd numeric NOT NULL, data jsonb NOT NULL,
    PRIMARY KEY(account_id, segment_id)
);
CREATE TABLE IF NOT EXISTS owner_restarts (
    account_id text REFERENCES accounts(account_id), action_id text, data jsonb NOT NULL,
    PRIMARY KEY(account_id, action_id)
);
CREATE TABLE IF NOT EXISTS risk_permits (
    account_id text REFERENCES accounts(account_id), nonce text, intent_id text NOT NULL,
    consumed boolean NOT NULL, revoked boolean NOT NULL, data jsonb NOT NULL,
    PRIMARY KEY(account_id, nonce)
);
CREATE TABLE IF NOT EXISTS outbox (
    account_id text REFERENCES accounts(account_id), intent_id text, client_order_id text NOT NULL,
    status text NOT NULL, data jsonb NOT NULL,
    PRIMARY KEY(account_id, intent_id), UNIQUE(account_id, client_order_id)
);
CREATE TABLE IF NOT EXISTS leadership (
    account_id text PRIMARY KEY REFERENCES accounts(account_id), holder text,
    token bigint NOT NULL CHECK(token >= 0), expires_at timestamptz, database_epoch text,
    last_clock_at timestamptz
);
CREATE TABLE IF NOT EXISTS reconciliation (
    account_id text PRIMARY KEY REFERENCES accounts(account_id), ready boolean NOT NULL,
    data jsonb NOT NULL
);
