import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from backend.config import ROOT
from backend.parttime_hours import match_hours
from backend.parttime_payroll import calculate_settlement
from backend.parttime_xlsx import read_sessions


class ParttimeParityTest(unittest.TestCase):
    def test_live_session_workbook_keeps_time_units_and_excel_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.xlsx"
            workbook = Workbook()
            workbook.active.append(["直播开始时间", "直播时长（秒）", "直播时长（分钟）", "标题"])
            workbook.active.append([datetime(2026, 8, 1, 9), 1200, 80, "Excel 日期"])
            workbook.active.append(["2026/8/1 23:50:45", None, 25, "跨午夜"])
            workbook.save(path)
            script = """
                import { readFile } from 'node:fs/promises';
                import { parseBackend } from './backend/adapters/attendance/attendanceCore.js';
                process.stdout.write(JSON.stringify(await parseBackend(await readFile(process.argv[1]), 'sports')));
            """
            old = subprocess.run(["node", "--input-type=module", "-e", script, str(path)], cwd=ROOT, check=True,
                                 capture_output=True, text=True, encoding="utf-8", env=os.environ | {"TZ": "Asia/Shanghai"})
            normalize = lambda rows: [{key: value for key, value in row.items() if key != "id"} for row in rows]
            self.assertEqual(normalize(read_sessions(path, "sports")), normalize(json.loads(old.stdout)))

    def test_workbook_rows_keep_the_legacy_hours_and_payroll_rules(self):
        day = "2026-08-01"

        def schedule(key, store, person, start, end, hours, role="主播"):
            return {"id": key, "date": day, "store_id": store, "store_name": store, "role": role,
                    "person": person, "start": start, "end": end, "planned_hours": hours}

        def session(key, store, start, end, hours, end_day=day):
            return {"id": key, "date": day, "store_id": store, "store_name": store,
                    "start": f"{day} {start}", "end": f"{end_day} {end}", "hours": hours, "source_row": 2}

        def operation(key, store, person, start, end, hours):
            return {"id": key, "date": day, "store_id": store, "store_name": store, "person": person,
                    "start": f"{day} {start}" if start else "", "end": f"{day} {end}" if end else "",
                    "operation_hours": hours, "source_sheet": "测试经营表", "source_row": 2}

        schedules = [
            schedule("a", "official", "样例甲", "09:00", "10:00", 1),
            schedule("a2", "official", "样例辛", "09:30", "09:45", 0.25),
            schedule("b", "official", "样例乙", "10:15", "11:00", 0.75),
            schedule("c", "official", "样例丙", "09:30", "11:00", 1.5, "中控"),
            schedule("d", "sports", "样例甲", "14:00", "15:00", 1),
            schedule("e", "sports", "样例壬", "15:00", "16:00", 1),
            schedule("f", "tmall", "样例丁", "23:00", "01:00", 2),
            schedule("g", "tmall", "样例戊", "19:00", "20:00", 1),
        ]
        backend = [
            session("one", "official", "08:45", "11:15", 2.5),
            session("two", "sports", "14:00", "16:15", 2.25),
            session("three", "tmall", "23:00", "01:15", 2.25, "2026-08-02"),
            session("four", "tmall", "19:00", "19:45", 0.75),
            session("five", "sports", "17:00", "18:00", 1),
        ]
        operations = [
            operation("op1", "official", "样例甲", "09:00", "10:00", 1.125),
            operation("op2", "sports", "样例己", "17:15", "17:45", 0.5),
            operation("op3", "tmall", "样例庚", "", "", 0.875),
        ]
        modes = ("backend_allocation", "payroll_protection")
        script = """
            import { analyze, settlementPolicy } from './backend/adapters/attendance/attendanceCore.js';
            let input = '';
            for await (const chunk of process.stdin.setEncoding('utf8')) input += chunk;
            const {schedule, backend, operations, modes} = JSON.parse(input);
            const expected = modes.map(timeCalculationMode => analyze(schedule, backend, operations, {timeCalculationMode, warningDiffHours: 0.25}));
            const attendance = { rows: [{role:'主播', alias:'样例甲', name:'合成核算名', rate:175}], payroll:[] };
            const settlement = { people: [{role:'主播', alias:'样例甲', name:'合成结算名'}] };
            const authoritative = analyze(schedule, backend, operations, {timeCalculationMode:'payroll_protection',
              warningDiffHours:0.25, attendanceOutput:attendance, settlementOutput:settlement});
            process.stdout.write(JSON.stringify({expected, attendance, settlement, authoritative, policy: settlementPolicy(schedule)}));
        """
        legacy = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True,
                                input=json.dumps({"schedule": schedules, "backend": backend, "operations": operations, "modes": modes}),
                                capture_output=True, text=True, encoding="utf-8", env=os.environ | {"TZ": "Asia/Shanghai"})
        reference = json.loads(legacy.stdout)

        def business_rows(rows):
            return [{key: value for key, value in row.items()
                     if key != "id" or not row.get("exclude_from_payroll")} for row in rows]

        for mode, expected in zip(modes, reference["expected"]):
            with self.subTest(mode=mode):
                matched = match_hours(schedules, backend, operations, mode=mode, warning_hours=0.25)
                self.assertEqual(business_rows(matched), business_rows(expected["schedule"]))
                calculated = calculate_settlement(matched, reference["policy"])
                self.assertEqual(calculated["computedPayroll"], expected["payroll"])
                self.assertEqual(calculated["systemSettlement"], expected["systemSettlement"])
                self.assertEqual(calculated["systemPayroll"], expected["systemPayroll"])

        matched = match_hours(schedules, backend, operations, mode="payroll_protection", warning_hours=0.25)
        authoritative = calculate_settlement(matched, reference["policy"], attendance=reference["attendance"],
                                             settlement=reference["settlement"])
        self.assertEqual(authoritative["systemSettlement"], reference["authoritative"]["systemSettlement"])
        self.assertEqual(authoritative["systemPayroll"], reference["authoritative"]["systemPayroll"])
