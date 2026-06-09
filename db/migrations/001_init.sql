-- 001_init.sql
-- Runs automatically on first Postgres startup via docker-entrypoint-initdb.d

BEGIN;

-- Least-privilege application role (not superuser)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pipeline_app') THEN
        CREATE ROLE pipeline_app WITH LOGIN PASSWORD 'pipeline_app_pwd';
    END IF;
END
$$;

-- ============================================================
-- field_mappings: stores the key translation layer
-- source_key  = what the upstream API currently sends
-- target_key  = what our internal schema expects
-- ============================================================
CREATE TABLE IF NOT EXISTS field_mappings (
    id          SERIAL PRIMARY KEY,
    source_name VARCHAR(128) NOT NULL,           -- e.g. "vendor_profile_api"
    version     INTEGER NOT NULL DEFAULT 1,
    source_key  VARCHAR(256) NOT NULL,
    target_key  VARCHAR(256) NOT NULL,
    value_type  VARCHAR(64)  NOT NULL DEFAULT 'string',  -- string, integer, float, bool, nested
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_name, version, source_key)
);

CREATE INDEX idx_mappings_active ON field_mappings (source_name, is_active)
    WHERE is_active = TRUE;

-- ============================================================
-- healing_audit: every self-heal event is logged here
-- the agent (Phase 3) writes; humans review
-- ============================================================
CREATE TABLE IF NOT EXISTS healing_audit (
    id              SERIAL PRIMARY KEY,
    source_name     VARCHAR(128) NOT NULL,
    old_version     INTEGER NOT NULL,
    new_version     INTEGER NOT NULL,
    raw_payload     JSONB NOT NULL,
    error_detail    TEXT NOT NULL,
    proposed_diff   JSONB NOT NULL,           -- {added: [], removed: [], changed: []}
    status          VARCHAR(32) NOT NULL DEFAULT 'pending',  -- pending | approved | applied | rejected
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at      TIMESTAMPTZ
);

-- ============================================================
-- Seed: initial v1 mapping for our mock vendor profile API
-- Maps the "good" /v1/profile response keys to internal keys
-- ============================================================
INSERT INTO field_mappings (source_name, version, source_key, target_key, value_type)
VALUES
    ('vendor_profile_api', 1, 'user_id',       'user_id',       'integer'),
    ('vendor_profile_api', 1, 'email_address',  'email_address', 'string'),
    ('vendor_profile_api', 1, 'full_name',      'full_name',     'string'),
    ('vendor_profile_api', 1, 'signup_date',    'signup_date',   'string'),
    ('vendor_profile_api', 1, 'is_active',      'is_active',     'bool'),
    ('vendor_profile_api', 1, 'address',        'address',       'nested')
ON CONFLICT DO NOTHING;

-- Grant least-privilege access
GRANT SELECT ON field_mappings TO pipeline_app;
GRANT SELECT, INSERT ON healing_audit TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE healing_audit_id_seq TO pipeline_app;

COMMIT;
