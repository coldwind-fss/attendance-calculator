# 部署与恢复

新版由一个 FastAPI 进程提供 Vue 工作台及 API。考勤计算复用现有 Python 算法和 Excel 适配器。绩效兼容进程由 FastAPI 启停，服务入口只绑定 `127.0.0.1:18405`。生产写入、历史迁移、回退分别按 本手册的发布顺序 执行。

## 发布输入

- Linux、systemd、Python 3.11 或更新版本，且 Python 内置 SQLite 3.38 或更新版本。Node 20.19 或更新版本满足当前前端构建要求。Python 运行时须位于服务用户可读取的系统目录。
- 通过验收的 Git 提交及其 `git archive` 导出。发布包包含源代码、锁定依赖清单、文档和部署文件。管理员凭据、原始业务数据、运行库、原始模板、签署件及历史归档通过服务器配置或历史迁移承接。
- 新部署使用空白全职模板和内置绩效办理页。`HR_FULLTIME_TEMPLATE` 和 `HR_PERFORMANCE_TEMPLATE_DIR` 可用于有定制模板的环境。新部署按需设置钉钉及部门策略，业务数据从空库开始。

## 安装

以根用户创建 `/etc/attendance-calculator`，将 `deploy/workbench.env.example` 安装为该目录下的 `workbench.env`，文件权限设为 `0600`，填入实际配置。外部发布使用独立 HTTPS 入口和 `HR_PUBLIC_BASE_URL`，员工通知、钉钉和 OA 先保持关闭。测试阶段可用 SSH 本地端口转发访问。

```bash
git archive --format=tar.gz --output=/tmp/attendance-release.tar.gz HEAD
bash deploy/install-release.sh /tmp/attendance-release.tar.gz "$(git rev-parse HEAD)" /path/to/python3.12
```

安装脚本在独立版本目录验证必需资产、安装依赖、构建 Vue 并运行后端用例，然后更新 `/opt/attendance-calculator/current`。服务由 `attendance-calculator` 系统用户运行，状态目录位于 `/var/lib/attendance-calculator`。就绪探针失败时脚本恢复之前的版本链接。数据库和工作簿回退使用 SP-08/09 核准的备份恢复点。

```bash
systemctl status attendance-calculator
curl -fsS http://127.0.0.1:18405/api/v1/health/ready
journalctl -u attendance-calculator -n 50
```

发布前在目标环境验证登录、兼职核算与下载、全职两种导入与版本发布、KPI 和 OKR 的办理与归档。通过后再按上线手册开启外部渠道并确认历史链接。
