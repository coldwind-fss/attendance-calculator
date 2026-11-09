from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel

from .database import utcnow
from .http import database, fail, problem_responses
from .session import Session, require_session


router = APIRouter(prefix="/attendance", tags=["attendance files"])
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_UPLOAD = 80 * 1024 * 1024
SOURCE_TYPES = {
    "parttime": {"schedule", "tmall_schedule", "backend", "operation"},
    "fulltime": {"workbook", "monthly", "raw"},
}


class SourceFile(BaseModel):
    file_id: str
    source_type: str
    filename: str
    sha256: str
    size: int


def get_source(connection, data_dir, actor, domain, source_id, source_type):
    row = connection.execute(
        "SELECT * FROM source_files WHERE id=? AND actor=? AND domain=? AND source_type=?",
        (source_id, actor, domain, source_type),
    ).fetchone()
    if row is None:
        fail(404, "SOURCE_NOT_FOUND", "找不到对应类型的源文件")
    return {"path": str(data_dir / "sources" / f"{row['id']}.xlsx"), "filename": row["filename"],
            "sha256": row["digest"]}


@router.post("/{kind}/source-files", status_code=201, response_model=SourceFile,
             responses=problem_responses(401, 403, 413))
def upload_source(kind: Literal["parttime", "fulltime"], request: Request,
                  file: UploadFile = File(), source_type: str = Form(),
                  session: Session = Depends(require_session)):
    if source_type not in SOURCE_TYPES[kind]:
        fail(422, "SOURCE_TYPE_INVALID", "源文件类型无效")
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if Path(filename).suffix.lower() != ".xlsx":
        fail(422, "WORKBOOK_REQUIRED", "请选择 .xlsx 工作簿")
    directory = request.app.state.settings.data_dir / "sources"
    directory.mkdir(exist_ok=True)
    file_id = uuid4().hex
    path = directory / f"{file_id}.xlsx"
    digest = sha256()
    size = 0
    try:
        with path.open("xb") as output:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    fail(413, "SOURCE_TOO_LARGE", "工作簿超过 80 MB")
                digest.update(chunk)
                output.write(chunk)
        if size == 0:
            fail(422, "SOURCE_EMPTY", "工作簿为空")
        checksum = digest.hexdigest()
        with database(request).connect() as connection:
            connection.execute("INSERT INTO source_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                file_id, kind, source_type, filename, checksum, size, session.actor, utcnow(),
            ))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return SourceFile(file_id=file_id, source_type=source_type, filename=filename, sha256=checksum, size=size)
