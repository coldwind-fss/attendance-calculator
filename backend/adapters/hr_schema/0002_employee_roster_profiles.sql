CREATE TABLE IF NOT EXISTS employee_roster_profiles (
  employee_id TEXT PRIMARY KEY,
  profile_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

PRAGMA optimize;
