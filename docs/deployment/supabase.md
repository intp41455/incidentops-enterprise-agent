# Supabase PostgreSQL 部署指南

本系统在底层数据持久化设计上采用无缝兼容架构：本地开发与轻量评测默认采用嵌入式 SQLite（零外部依赖、开箱即用）；在生产级多租户或云原生部署场景下，原生支持直连 Supabase / PostgreSQL 数据库集群。

## 1. 快速接入步骤

### 第一步：获取 Supabase 连接串
1. 登录 Supabase 控制台 (https://supabase.com/dashboard)；
2. 新建或进入已有项目，进入 Project Settings -> Database；
3. 复制 Connection string (URI)。

### 第二步：执行架构迁移与 RLS 策略
在 Supabase 控制台的 SQL Editor 中运行项目提供的初始化脚本：
- supabase/migrations/20260928000001_init_schema.sql

该脚本将自动完成：
- 5 大核心业务与审计表创建（import_jobs, job_logs, files, ticket_records, audit_ledger）；
- 严苛的唯一键约束（uq_tenant_idempotency 防止重复扣费或重试数据击穿）；
- 开启 PostgreSQL 行级安全 (Row-Level Security, RLS)，绑定 app.current_tenant 会话变量实现物理级隔离。

### 第三步：配置环境变量并启动
在项目根目录创建或修改 .env 文件：
DATABASE_URL=postgresql://postgres:[password]@db.[ref].supabase.co:5432/postgres

启动生产后端：
python -m uvicorn services.api.main:app --host 0.0.0.0 --port 8000

## 2. 多租户安全与防击穿设计
- 数据库级 RLS 提供深层纵深防御，与服务层闭包租户注入互为双重校验。
- ticket_records 表的 (tenant_id, idempotency_key) 唯一约束确保即使前端并发重发或重放，底层数据库仅执行一次，返回历史工单记录。
