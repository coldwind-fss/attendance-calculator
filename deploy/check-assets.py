"""Verify that the full workflow can be started with the installed assets."""

from pathlib import Path
from shlex import split
import sys

from openpyxl import load_workbook


def main(environment: Path):
    settings = dict(word.split("=", 1) for word in split(environment.read_text(encoding="utf-8"), comments=True))
    required = ("HR_ADMIN_USERNAME", "HR_ADMIN_PASSWORD")
    missing = [key for key in required if not settings.get(key)]
    if settings.get("HR_ADMIN_PASSWORD") == "replace-before-install":
        missing.append("HR_ADMIN_PASSWORD (replace the example password)")
    if settings.get("HR_ENABLE_PERFORMANCE_COMPAT") != "1":
        missing.append("HR_ENABLE_PERFORMANCE_COMPAT=1")
    if missing:
        raise SystemExit("Missing release settings: " + ", ".join(missing))

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
