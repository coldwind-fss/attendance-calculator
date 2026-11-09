import json
import os
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY_ATTENDANCE = ROOT / "backend" / "adapters" / "attendance"
LEGACY_FULLTIME = LEGACY_ATTENDANCE / "fulltime"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    admin_username: str = ""
    admin_password: str = ""
    secure_cookie: bool = True
    fulltime_template: Path | None = None
    performance_compat: bool = False
    performance_accounts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_environment(cls):
        template = os.getenv("HR_FULLTIME_TEMPLATE")
        return cls(
            data_dir=Path(os.getenv("HR_DATA_DIR", ROOT / ".data" / "workbench")).resolve(),
            admin_username=os.getenv("HR_ADMIN_USERNAME", ""),
            admin_password=os.getenv("HR_ADMIN_PASSWORD", ""),
            secure_cookie=os.getenv("HR_LOCAL_DEVELOPMENT") != "1",
            fulltime_template=Path(template).resolve() if template else None,
            performance_compat=os.getenv("HR_ENABLE_PERFORMANCE_COMPAT") == "1",
            performance_accounts=json.loads(os.getenv("HR_PERFORMANCE_ACCOUNTS", "{}")),
        )
