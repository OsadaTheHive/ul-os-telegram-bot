-- Audit log migration (Postgres po Q3 2026 SQLite -> Postgres migracji).
-- Per Vault proposal: 00 — META/PROPOSALS/2026-05-09_audit_log_schema.md
--
-- Uruchom: psql -h cms.osadathehive.pl -U directus -d directus < audit_log_migration.sql
-- Albo przez Directus admin UI > Database > SQL.

BEGIN;

-- Audit log table - osobna od directus_revisions
CREATE TABLE IF NOT EXISTS audit_log (
    -- Identity
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,

    -- Multi-tenant (per ADR-008)
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'hivelive_ecosystem',

    -- Who
    actor_type VARCHAR(32) NOT NULL,
    actor_id VARCHAR(128),
    actor_label VARCHAR(255),
    ip_address INET,
    user_agent TEXT,
    session_id UUID,

    -- What
    action VARCHAR(64) NOT NULL,

    -- On what
    resource_type VARCHAR(64),
    resource_id VARCHAR(128),
    resource_label VARCHAR(255),

    -- How / What changed
    method VARCHAR(16),
    path VARCHAR(512),
    status_code INT,
    before_data JSONB,
    after_data JSONB,
    diff JSONB,

    -- Result
    result VARCHAR(32) NOT NULL,
    error_code VARCHAR(64),
    error_message TEXT,

    -- Performance
    duration_ms INT,
    tokens_used INT,

    -- Tamper-proof chain (opcjonalne)
    prev_event_hash VARCHAR(64),
    event_hash VARCHAR(64),

    -- Extra
    metadata JSONB,

    -- Time
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Constraints
    CONSTRAINT audit_log_actor_type_chk CHECK (actor_type IN (
        'user', 'service', 'worker', 'mcp', 'bot', 'system', 'oauth_client'
    )),
    CONSTRAINT audit_log_result_chk CHECK (result IN (
        'ok', 'denied', 'error', 'rate_limited', 'partial'
    ))
);

-- Indeksy pod typowe zapytania
CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON audit_log (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor_time ON audit_log (actor_id, created_at DESC) WHERE actor_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_log (resource_type, resource_id) WHERE resource_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_action_time ON audit_log (action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_result ON audit_log (result, created_at DESC) WHERE result != 'ok';
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log (session_id) WHERE session_id IS NOT NULL;

-- Append-only - prevent UPDATE/DELETE z aplikacji (super_admin moze przez audit)
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY audit_tenant_isolation ON audit_log
    FOR ALL
    USING (
        tenant_id = current_setting('app.current_tenant_id', true)
        OR current_setting('app.is_super_admin', true) = 'true'
    );

CREATE POLICY audit_no_update ON audit_log FOR UPDATE USING (false);

CREATE POLICY audit_no_delete ON audit_log FOR DELETE USING (
    current_setting('app.is_super_admin', true) = 'true'
);

-- Insert tylko z poprawnym tenant_id
CREATE POLICY audit_insert ON audit_log FOR INSERT WITH CHECK (
    tenant_id = current_setting('app.current_tenant_id', true)
    OR current_setting('app.is_super_admin', true) = 'true'
);

-- Komentarze dla Directus admin UI
COMMENT ON TABLE audit_log IS 'Append-only audit log per RODO/SaaS. Osobno od directus_revisions. Czas hot 90 dni, potem archiwizacja do HOS.';
COMMENT ON COLUMN audit_log.tenant_id IS 'Per-tenant isolation - filtrowane przez RLS';
COMMENT ON COLUMN audit_log.actor_type IS 'user|service|worker|mcp|bot|system|oauth_client';
COMMENT ON COLUMN audit_log.action IS 'login.success|login.failure|doc.created|doc.updated|doc.deleted|file.downloaded|...';
COMMENT ON COLUMN audit_log.before_data IS 'Snapshot before update';
COMMENT ON COLUMN audit_log.after_data IS 'Snapshot after update';
COMMENT ON COLUMN audit_log.event_hash IS 'sha256 calego eventu - tamper-proof chain';
COMMENT ON COLUMN audit_log.prev_event_hash IS 'Hash poprzedniego eventu - tamper-proof chain';

-- Helper view: ostatnie 1000 events per tenant (do dashboards)
CREATE OR REPLACE VIEW audit_log_recent AS
SELECT
    id, event_id, tenant_id,
    actor_type, actor_label, ip_address,
    action, resource_type, resource_label,
    result, error_message,
    duration_ms, created_at
FROM audit_log
ORDER BY created_at DESC
LIMIT 1000;

COMMIT;

-- ROLLBACK plan (jezeli cos sie zepsuje):
-- BEGIN;
-- DROP VIEW IF EXISTS audit_log_recent;
-- DROP TABLE IF EXISTS audit_log CASCADE;
-- COMMIT;
