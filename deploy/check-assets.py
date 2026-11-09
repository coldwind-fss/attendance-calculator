"""Verify that the full workflow can be started with the installed assets."""

from pathlib import Path
from shlex import split
import sys

from openpyxl import load_workbook


def main(environment: Path):
    settings = dict(word.split("=", 1) for word in split(environment.read_text(encoding="utf-8"), comments=True))
    if settings.get("HR_ENABLE_PERFORMANCE_COMPAT") != "1":
        raise SystemExit("Set HR_ENABLE_PERFORMANCE_COMPAT=1 to include the performance demo")

    if template := settings.get("HR_FULLTIME_TEMPLATE"):
        with Path(template).open("rb") as stream:
            workbook = load_workbook(stream, read_only=True)
            try:
                if "全职" not in workbook.sheetnames:
                    raise SystemExit("Full-time workbook template has no 全职 sheet")
            finally:
                workbook.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
