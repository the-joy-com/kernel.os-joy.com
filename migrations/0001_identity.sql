-- Identity: the human symbiot, the one-time login codes issued to them,
-- and the sessions a spent code mints.
-- The schema_migrations ledger itself is created by the runner (db.py), not here,
-- so this file is pure domain schema.

-- The one human behind The Joy.
-- Exactly one row in practice, seeded from SYMBIOT_EMAIL at startup.
-- Today the single seeded address;
-- the schema and /login already support more than one,
-- so this is the first symbiot, not the only one there can ever be.
CREATE TABLE symbiot (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email      TEXT        NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A one-time login code.
-- code_hash is HMAC(code) — the plaintext never lands.
-- consumed_at marks a code spent; expires_at bounds its life.
-- Only the latest unconsumed, unexpired code for a symbiot is ever accepted.
-- failed_attempts counts wrong guesses against this code;
-- once it reaches the configured budget the code is burned (consumed_at set),
-- so brute force is a bounded thing the row enforces, not a race the search space happens to win.
CREATE TABLE login_code (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbiot_id     BIGINT      NOT NULL REFERENCES symbiot (id) ON DELETE CASCADE,
    code_hash      TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    consumed_at    TIMESTAMPTZ,
    failed_attempts INTEGER    NOT NULL DEFAULT 0
);
CREATE INDEX login_code_symbiot_idx ON login_code (symbiot_id);

-- At most one spendable login code per symbiot —
-- enforced by the database,
-- so "only the latest code works" is a constraint, not a timing assumption.
-- Two overlapping /login calls can't interleave into two live codes:
-- the row layer forbids a second unconsumed code from existing,
-- and issuance overwrites the single live row in place (see issue_login_code's upsert).
-- "Spendable" means unconsumed;
-- verify additionally requires unexpired,
-- but expiry is time-relative and can't sit in an index predicate,
-- so the index keys on the consumed flag.
CREATE UNIQUE INDEX login_code_one_live_per_symbiot
    ON login_code (symbiot_id)
    WHERE consumed_at IS NULL;

-- An authenticated session.
-- token_hash is HMAC(token);
-- the plaintext token is returned to the client once and never stored.
-- revoked_at marks a logout.
CREATE TABLE session (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbiot_id BIGINT      NOT NULL REFERENCES symbiot (id) ON DELETE CASCADE,
    token_hash TEXT        NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);
