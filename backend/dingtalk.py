import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .jobs import JobFailure


CHINA = timezone(timedelta(hours=8))
HR_FIELDS = ("sys00-name", "sys00-deptIds", "sys00-position", "sys00-confirmJoinTime", "sys01-employeeType",
             "sys01-employeeStatus", "sys01-regularTime", "sys01-planRegularTime", "sys02-certNo")


def post(url, data):
    request = Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=10) as response:
            return json.load(response)
    except (URLError, TimeoutError, ValueError):
        raise JobFailure("钉钉接口暂不可用，请检查应用权限和网络") from None


def date_value(value):
    raw = str(value or "")
    if raw.isascii() and raw.isdigit() and len(raw) in (10, 13):
        return datetime.fromtimestamp(int(raw) / (1000 if len(raw) == 13 else 1), CHINA).date().isoformat()
    return raw.replace("/", "-")[:10]


def mask_certificate(raw):
    raw = raw.strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return raw[:2] + "****" + raw[-2:]
    return raw[:4] + "*" * min(10, len(raw) - 8) + raw[-4:]


def employee_ids(call, suffix, extra):
    ids, cursor = [], 0
    for _ in range(20):
        result = call(f"topapi/smartwork/hrm/employee/{suffix}", extra | {"offset": cursor, "size": 50})
        batch = result.get("data_list", [])
        ids.extend(batch)
        following = result.get("next_cursor")
        if not batch or following is None or following == cursor:
            break
        cursor = following
    return list(dict.fromkeys(ids))


def fetch_organization(scope):
    app_key = os.getenv("DINGTALK_CLIENT_ID")
    app_secret = os.getenv("DINGTALK_CLIENT_SECRET")
    agent_id = os.getenv("DINGTALK_AGENT_ID", "")
    if not app_key or not app_secret or not agent_id.isdigit():
        raise JobFailure("尚未配置钉钉智能人事应用")
    token = post("https://api.dingtalk.com/v1.0/oauth2/accessToken", {"appKey": app_key, "appSecret": app_secret}).get("accessToken")
    if not token:
        raise JobFailure("无法获取钉钉访问令牌")

    def call(endpoint, data):
        result = post(f"https://oapi.dingtalk.com/{endpoint}?{urlencode({'access_token': token})}", data)
        if result.get("errcode") != 0 or "result" not in result:
            raise JobFailure("钉钉智能人事接口未返回结果，请检查权限")
        return result["result"]

    def department_details(department_id):
        return call("topapi/v2/department/get", {"dept_id": department_id, "language": "zh_CN"})

    def children(department_id):
        return call("topapi/v2/department/listsub", {"dept_id": department_id, "language": "zh_CN"})

    active = employee_ids(call, "queryonjob", {"status_list": "2,3,5,-1"})
    departments, departures = {}, {}
    organization_name = "钉钉组织" if scope == "current" else ""
    with ThreadPoolExecutor(max_workers=8) as pool:
        if scope == "current":
            ids = active
            try:
                organization_name = department_details(1).get("name", organization_name)
            except JobFailure:
                pass  # The root name is optional in the source application.
            pending, visited = [1], set()
            while pending and len(visited) < 200:
                batch, pending = pending[:8], pending[8:]
                batch = [key for key in dict.fromkeys(batch) if key not in visited]
                visited.update(batch)
                for group in pool.map(children, batch):
                    for department in group:
                        key = department["dept_id"]
                        departments[key] = department
                        if key not in visited:
                            pending.append(key)
            details = dict(zip(departments, pool.map(department_details, departments)))
        else:
            active_set = set(active)
            ids = [value for value in employee_ids(call, "querydimission", {}) if value not in active_set]
            details = {}
            try:
                for start in range(0, len(ids), 50):
                    for row in call("topapi/smartwork/hrm/employee/listdimission", {"userid_list": ",".join(ids[start:start + 50])}):
                        departures[row["userid"]] = row.get("last_work_day")
            except JobFailure:
                pass  # Detail access is optional for departed employees.

        def load_records(start):
            return call("topapi/smartwork/hrm/employee/v2/list", {
                "agentid": int(agent_id), "userid_list": ",".join(ids[start:start + 100]),
                "field_filter_list": ",".join(HR_FIELDS),
            })

        records = [record for batch in pool.map(load_records, range(0, len(ids), 100)) for record in batch]

    excluded_ids = {value.strip() for value in os.getenv("DINGTALK_EXCLUDED_EMPLOYEE_IDS", "").split(",")}
    excluded_names = {value.strip() for value in os.getenv("DINGTALK_EXCLUDED_EMPLOYEE_NAMES", "").split(",")}
    memberships = json.loads(os.getenv("DINGTALK_MEMBERSHIP_EXCLUSIONS", "{}"))
    manager_overrides = json.loads(os.getenv("DINGTALK_MANAGER_OVERRIDES", "{}"))
    department_overrides = json.loads(os.getenv("DINGTALK_DEPARTMENT_NAME_OVERRIDES", "{}"))
    employees = []
    for record in records:
        values = {field["field_code"]: [item.get("label") or item.get("value", "")
                                       for item in field.get("field_value_list", [])]
                  for field in record.get("field_data_list", [])}

        def first(key):
            return next((value for value in values.get(key, []) if value), "")

        user_id, name = record.get("userid"), first("sys00-name")
        if not user_id or user_id in excluded_ids or name in excluded_names or not name:
            continue
        member_ids = [part.strip() for value in values.get("sys00-deptIds", []) for part in re.split(r"[|,]", value)]
        member_ids = ["1" if item == "-1" else item for item in member_ids if item]
        employment_type = first("sys01-employeeType")
        employees.append({
            "id": user_id, "name": name, "nickname": "", "title": first("sys00-position"),
            "departmentIds": [item for item in member_ids if item not in memberships.get(user_id, [])],
            "employeeType": employment_type, "employeeStatus": first("sys01-employeeStatus"),
            "rosterStatus": "离职" if scope == "departed" else "兼职" if "兼职" in employment_type else "在职",
            "hireDate": date_value(first("sys00-confirmJoinTime")),
            "regularDate": date_value(first("sys01-regularTime") or first("sys01-planRegularTime")),
            "certificateNoMasked": mask_certificate(first("sys02-certNo")),
            "departureDate": date_value(departures.get(user_id)),
        })

    names_by_id = {item["id"]: item["name"] for item in employees}
    normalized_departments = []
    for key, item in departments.items():
        name = item["name"]
        manager_ids = details[key].get("dept_manager_userid_list", [])
        if isinstance(manager_ids, str):
            manager_ids = [value.strip() for value in manager_ids.split(",") if value.strip()]
        matches = [employee_id for employee_id, employee_name in names_by_id.items() if employee_name == manager_overrides.get(name)]
        if len(matches) == 1:
            manager_ids = matches
        normalized_departments.append({"id": str(key), "name": department_overrides.get(name, name),
                                       "parentId": str(item.get("parent_id", 1)), "order": item.get("order", 0),
                                       "managerIds": manager_ids,
                                       "managerNames": [names_by_id[value] for value in manager_ids if value in names_by_id]})
    return {"source": "钉钉智能人事花名册", "organizationName": organization_name,
            "departments": normalized_departments, "employees": employees}
