# 考勤计算与 HR 工作台

Vue 3 提供工作台，FastAPI 提供会话、文件、业务接口、作业、数据存储和事件。系统从空库启动。仓库包含源码、数据库结构、验证样例和部署文件。

[产品基线](docs/PRODUCT_BASELINE.md) · [技术基线](docs/TECHNICAL_BASELINE.md) · [部署说明](docs/DEPLOYMENT.md)

## 本地运行

需要 Python 3.11 或更新版本、SQLite 3.38 或更新版本，以及 Node 20.19 或更新版本。

```bash
python -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
npm ci --prefix backend/adapters/attendance
npm ci --prefix frontend
npm run --prefix frontend build
export HR_LOCAL_DEVELOPMENT=1 HR_ADMIN_USERNAME=attendance-admin HR_ENABLE_PERFORMANCE_COMPAT=1
read -rsp 'Local administrator password: ' HR_ADMIN_PASSWORD
export HR_ADMIN_PASSWORD
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Windows PowerShell 使用 `.venv\Scripts\python.exe`，通过 `$env:` 设置环境变量。浏览器访问 `http://127.0.0.1:8000`，API 说明在 `/docs`。执行 `python -m unittest discover -s backend/tests -v` 验证主要业务路径。

## 模块和流程

| 模块 | 工作流 | 代码 |
| --- | --- | --- |
| 人事 | 同步组织与人员、编辑档案、调整编制、筛选与处理招聘需求 | `backend/hr.py`、`frontend/src/HumanResources.vue` |
| 兼职 | 选择月份、上传排班与场次、核算工时和工资、查看异常与导出 | `backend/parttime*.py`、`frontend/src/Parttime.vue` |
| 全职 | 整表或双源导入、计算、修订、版本化发布、通知与回执 | `backend/fulltime.py`、`backend/notices.py`、`frontend/src/Fulltime.vue` |
| 绩效 | 按月建立 KPI／OKR，逐角色办理、核验签署、复核归档和查询部门审批 | `backend/performance*.py`、`frontend/src/Performance.vue` |

## 技术结构与数据

- `frontend/` 是 Vue 3 工作台。FastAPI 提供静态构建及 `/api/v1` 管理接口，员工考勤链接通过 `/api/public/v1` 办理。
- `backend/` 管理权限、流程、算法和存储。`backend/adapters/` 隔离 Excel 适配、全职计算和绩效签署／钉钉适配。Node 负责兼容格式和绩效办理渲染，FastAPI 统一对外入口。
- SQLite 保存组织快照、档案、考勤运行与版本、作业和事务事件。工作簿与绩效归档存放在 `HR_DATA_DIR` 下。代码通过初始化脚本创建数据结构，数据目录默认是 `.data/workbench`。
- 登录会话使用 Cookie，管理写请求包含 `X-CSRF-Token`。异步计算返回作业 ID。事件字段通过登录后访问 `/api/v1/event-schema` 查询，全职工作台可按月份订阅变更。
- 真实人员、店铺费率例外、设备、钉钉配置和业务数据由实际环境配置。详见 [环境和业务策略](docs/POLICY_CONFIGURATION.md) 与 [部署说明](docs/DEPLOYMENT.md)。
