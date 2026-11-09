from http import HTTPStatus

from fastapi import HTTPException, Request
from pydantic import BaseModel


class Problem(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    code: str
    request_id: str


def problem_responses(*statuses):
    content = {"application/problem+json": {"schema": Problem.model_json_schema()}}
    return {status: {"description": HTTPStatus(status).phrase, "content": content} for status in statuses}


def fail(status: int, code: str, message: str):
    raise HTTPException(status, {"code": code, "title": message})


def database(request: Request):
    return request.app.state.database
