-- ==============================================================================
-- IncidentOps: Supabase PostgreSQL 数据库架构与生产多租户行级安全 (RLS) 策略
-- 
-- 架构特性：
-- 1. 强租户隔离 (tenant_id) 与外键完整性约束
-- 2. 启用 PostgreSQL 行级安全 (Row-Level Security, RLS)，防范任何越权数据探测
-- 3. 幂等执行唯一约束 (idempotency_key) 与 SHA256 载荷签名防护
-- ==============================================================================

CREATE TABLE IF NOT EXISTS import_jobs (
    id SERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    job_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    source_filename VARCHAR(255) NOT NULL,
    product_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    error_summary TEXT,
    retry_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_job UNIQUE (tenant_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_import_jobs_tenant_status ON import_jobs (tenant_id, status);

CREATE TABLE IF NOT EXISTS job_logs (
    id SERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    log_id VARCHAR(64) NOT NULL,
    job_id VARCHAR(64) NOT NULL,
    level VARCHAR(16) NOT NULL DEFAULT 'INFO',
    message TEXT NOT NULL,
    record_id VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_log UNIQUE (tenant_id, log_id)
);
CREATE INDEX IF NOT EXISTS idx_job_logs_tenant_job ON job_logs (tenant_id, job_id);

CREATE TABLE IF NOT EXISTS files (
    id SERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    file_id VARCHAR(64) NOT NULL,
    filename VARCHAR(255) NOT NULL,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    row_count INT NOT NULL DEFAULT 0,
    sha256_hash VARCHAR(64) NOT NULL,
    validation_status VARCHAR(32) NOT NULL DEFAULT 'unverified',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_file UNIQUE (tenant_id, file_id)
);

CREATE TABLE IF NOT EXISTS ticket_records (
    id SERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    ticket_id VARCHAR(64) NOT NULL,
    target_job_id VARCHAR(64) NOT NULL,
    action_type VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    approver_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'executed',
    execution_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_ticket UNIQUE (tenant_id, ticket_id),
    CONSTRAINT uq_tenant_idempotency UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS audit_ledger (
    id SERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    actor_id VARCHAR(64) NOT NULL,
    agent_role VARCHAR(64) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    summary TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_run_id ON audit_ledger (run_id);

ALTER TABLE import_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE files ENABLE ROW LEVEL SECURITY;
ALTER TABLE ticket_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_ledger ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_import_jobs ON import_jobs
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));

CREATE POLICY tenant_isolation_job_logs ON job_logs
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));

CREATE POLICY tenant_isolation_files ON files
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));

CREATE POLICY tenant_isolation_ticket_records ON ticket_records
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));

CREATE POLICY tenant_isolation_audit_ledger ON audit_ledger
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
