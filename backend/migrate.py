"""Create an isolated workbench candidate from read-only legacy exports."""

import argparse
import json
import shutil
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from . import fulltime, hr
from .database import Database, json_text


HR_TABLES = ("data_cache", "employee_roster_profiles", "staffing_plans", "recruitment_requests",
             "recruitment_approvals", "recruitment_monthly_snapshots")
FULLTIME_TABLES = ("attendance_months", "calculation_runs", "result_versions", "corrections", "notices",
                   "feedback_cases", "notification_deliveries", "employee_recipient_mappings",
                   "notification_exclusions", "audit_logs", "algorithm_rule_changes")
PERFORMANCE_FILES = frozenset((
    "selfeval_data.json", "mgrscore_data.json", "bpscore_data.json", "kpi_confirm_data.json",
    "result_confirm_data.json", "kpi_targets.json", "kpi_target_drafts.json", "roster.json",
    "assessment_roster.json", "monthly_events.json", "okr_workflows.json", "workflow_resets.json",
    "oa_approval_data.json", "performance_exclusions.json", "signature_audit.jsonl", "employee_overrides.json",
))


def read_database(path):
    source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    source.execute("BEGIN")
    return source


def copy_tables(source, destination, tables, remap=lambda row: row):
    counts = {}
    present = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in tables:
        if table not in present:
            counts[table] = None
            continue
        allowed = {row[1] for row in destination.execute(f"PRAGMA table_info({table})")}
        records = source.execute(f"SELECT * FROM {table}")
        columns = [field[0] for field in records.description if field[0] in allowed]
        quoted = ",".join(f'"{column}"' for column in columns)
        parameters = ",".join("?" for column in columns)
        counts[table] = destination.executemany(f"INSERT INTO {table} ({quoted}) VALUES ({parameters})", (
            tuple(row[column] for column in columns) for row in (remap(dict(record)) for record in records)
        )).rowcount
    return counts


def import_fulltime(source, destination, workbook_dir, stage, target):
    schema = source.execute("SELECT value FROM platform_meta WHERE key='schema_version'").fetchone()
    if schema is None or str(schema[0]) != "7":
        raise ValueError("全职来源需为 SQLite schema 7")
    copied = set()
    folder = Path("fulltime") / "workbooks"
    (stage / folder).mkdir(parents=True)

    def remap(row):
        if "workbook_path" not in row:
            return row
        name = PureWindowsPath(row["workbook_path"]).name
        if name not in copied:
            source_file = workbook_dir / name
            if not source_file.is_file():
                raise ValueError("全职历史工作簿存在缺失，请核对来源清单")
            shutil.copyfile(source_file, stage / folder / name)
            copied.add(name)
        return row | {"workbook_path": str(target / folder / name)}

    counts = copy_tables(source, destination, FULLTIME_TABLES, remap)
    broken = destination.execute("""
        SELECT 1 FROM attendance_months m
        WHERE (m.current_run_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM calculation_runs r WHERE r.id=m.current_run_id AND r.month_id=m.id))
        OR (m.current_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM result_versions v WHERE v.id=m.current_version_id AND v.month_id=m.id))
        OR (m.published_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM result_versions v WHERE v.id=m.published_version_id AND v.month_id=m.id))
    """).fetchone()
    if broken:
        raise ValueError("全职月份存在缺失的批次或版本指针")
    return {"tables": counts, "workbooks": len(copied)}


def performance_entry(name):
    path = PurePosixPath(name)
    if ("\\" in name or ":" in name or path.is_absolute() or ".." in path.parts or path.as_posix() != name
            or not path.parts or path.parts[0] != "data" or any(part.startswith(".") for part in path.parts)):
        raise ValueError("绩效 ZIP 中含有不安全的路径")
    relative = PurePosixPath(*path.parts[1:])
    if len(relative.parts) == 1 and relative.name in PERFORMANCE_FILES:
        return relative
    if len(relative.parts) >= 2 and relative.parts[0] in ("archives", "generated-pages") and relative.suffix.lower() == ".html":
        return relative
    raise ValueError("绩效 ZIP 中包含未列入迁移的文件，请提供业务数据导出包")


def import_performance(path, destination):
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        records = manifest["files"]
        if isinstance(records, dict):
            records = [metadata | {"path": name} for name, metadata in records.items()]
        expected = {item["path"]: item for item in records}
        actual = [info.filename for info in archive.infolist() if not info.is_dir() and info.filename != "manifest.json"]
        if set(actual) != expected.keys() or len(actual) != len(expected) or len(records) != len(expected):
            raise ValueError("绩效 ZIP 的清单与文件不一致")
        paths = {name: performance_entry(name) for name in actual}
        if len({item.as_posix().casefold() for item in paths.values()}) != len(paths):
            raise ValueError("绩效 ZIP 中存在重名文件")
        counts = {"files": 0, "archive_pages": 0, "generated_pages": 0, "signature_entries": 0}
        for name, relative in paths.items():
            content = archive.read(name)
            entry = expected[name]
            if len(content) != entry["size"] or sha256(content).hexdigest() != entry["sha256"].lower():
                raise ValueError("绩效 ZIP 文件未通过 manifest 校验")
            if relative.suffix == ".json":
                json.loads(content.decode("utf-8-sig"))
            if relative.suffix == ".jsonl":
                for line in content.decode("utf-8-sig").splitlines():
                    if line.strip():
                        json.loads(line)
                        counts["signature_entries"] += 1
            output = destination.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
            counts["files"] += 1
            counts["archive_pages"] += relative.parts[0] == "archives"
            counts["generated_pages"] += relative.parts[0] == "generated-pages"
        return counts


def migrate(target, hr_db=None, fulltime_db=None, fulltime_workbooks=None, performance_zip=None, roster_json=None):
    target = target.resolve()
    if target.exists():
        raise ValueError("迁移目标须为新的隔离目录")
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="hr-migration-", dir=target.parent) as directory:
        stage = Path(directory) / "candidate"
        db = Database(stage / "workbench.sqlite")
        db.initialize()
        fulltime.initialize(db)
        hr.initialize(db)
        report = {}
        with db.connect() as destination:
            if hr_db:
                with closing(read_database(hr_db)) as source:
                    report["hr"] = copy_tables(source, destination, HR_TABLES)
            if fulltime_db:
                with closing(read_database(fulltime_db)) as source:
                    report["fulltime"] = import_fulltime(source, destination, fulltime_workbooks, stage, target)
            if roster_json:
                roster = json.loads(roster_json.read_text(encoding="utf-8-sig"))
                destination.executemany("INSERT INTO fulltime_roster VALUES (?, ?)",
                                       ((i, json_text(item)) for i, item in enumerate(roster)))
                report["fulltime_roster_count"] = len(roster)
        if performance_zip:
            report["performance"] = import_performance(performance_zip, stage / "performance")
        (stage / "migration-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        stage.rename(target)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--hr-db", type=Path)
    parser.add_argument("--fulltime-db", type=Path)
    parser.add_argument("--fulltime-workbooks", type=Path)
    parser.add_argument("--performance-zip", type=Path)
    parser.add_argument("--roster-json", type=Path)
    arguments = parser.parse_args()
    if arguments.fulltime_db and not arguments.fulltime_workbooks:
        parser.error("--fulltime-db requires --fulltime-workbooks")
    if not any((arguments.hr_db, arguments.fulltime_db, arguments.performance_zip, arguments.roster_json)):
        parser.error("provide at least one legacy source")
    try:
        print(json.dumps(migrate(**vars(arguments)), ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"迁移未生成候选目录：{error}\n")


if __name__ == "__main__":
    main()
