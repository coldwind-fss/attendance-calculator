CREATE TABLE IF NOT EXISTS staffing_plans (
  department_id TEXT PRIMARY KEY,
  department_name TEXT NOT NULL,
  approved_headcount INTEGER NOT NULL CHECK (approved_headcount >= 0),
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recruitment_requests (
  id TEXT PRIMARY KEY,
  department_id TEXT NOT NULL,
  department_name TEXT NOT NULL,
  position_name TEXT NOT NULL,
  demand_type TEXT NOT NULL CHECK (demand_type IN ('新增编制','离职补缺','优化替换','其他')),
  status TEXT NOT NULL CHECK (status IN ('RECRUITING','URGENT','PENDING_APPROVAL','CLOSED')),
  headcount_needed INTEGER NOT NULL DEFAULT 1 CHECK (headcount_needed > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recruitment_approvals (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recruitment_monthly_snapshots (
  month TEXT PRIMARY KEY,
  open_positions INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recruitment_requests_type_status
ON recruitment_requests (demand_type, status);

CREATE INDEX IF NOT EXISTS idx_recruitment_requests_created_at
ON recruitment_requests (created_at);

CREATE INDEX IF NOT EXISTS idx_recruitment_approvals_status
ON recruitment_approvals (status);

PRAGMA optimize;
