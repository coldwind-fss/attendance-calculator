import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY_ATTENDANCE = ROOT / "backend" / "adapters" / "attendance"
LEGACY_FULLTIME = LEGACY_ATTENDANCE / "fulltime"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    secure_cookie: bool = True
    fulltime_template: Path | None = None
    performance_compat: bool = False

    @classmethod
    def from_environment(cls):
        template = os.getenv("HR_FULLTIME_TEMPLATE")
        return cls(
            data_dir=Path(os.getenv("HR_DATA_DIR", ROOT / ".data" / "workbench")).resolve(),
            secure_cookie=os.getenv("HR_LOCAL_DEVELOPMENT") != "1",
            fulltime_template=Path(template).resolve() if template else None,
            performance_compat=os.getenv("HR_ENABLE_PERFORMANCE_COMPAT") == "1",
        )
