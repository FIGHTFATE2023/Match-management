# 比赛管理系统 · 追分版

基于 Flask、SQLite、Jinja2 和原生 JavaScript 的比赛管理系统，支持分组名单、队长出场安排、赛程发布、裁判计分及积分榜。

## 环境

- Python 3.10 或更高版本
- 依赖见 `requirements.txt`

## Windows 启动

双击 `start.cmd`，启动脚本会检查并安装 Flask。默认访问：

<http://127.0.0.1:8768>

也可以手动启动：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

可通过环境变量 `HOST` 和 `PORT` 调整监听地址与端口。

## 公网入口

运行 `start-public.cmd`，默认通过 Cloudflare 临时隧道转发到本地服务。也可运行 `start-public.cmd --tunnel ngrok`，使用 ngrok；该方式需要自行配置 `.local/ngrok.yml`，固定入口设置见 `public_tunnel.py`。

本机、局域网和公网入口共用同一个后端与数据库。公网脚本会复用已运行的本地服务。

## 数据与账号

首次启动自动生成 `.local/match.db` 和 `.local/secret_key.txt`，并写入 `db.py` 中的初始分组名单。后续名单、安排及计分以数据库为准。

选手/观众使用姓名和个人四位验证码登录，初始验证码为 `1234`，可在登录后修改。老师入口按现有业务规则选择身份登录。

`.local/` 中包含运行数据、密钥和隧道配置，已由 `.gitignore` 排除。虚拟环境、缓存和本地工具状态也不上传。需要保留现有比赛进度时，请另行备份 `.local/`。

## 文件结构

- `app.py`：Flask 路由、登录与应用入口
- `db.py`：SQLite 数据模型和比赛规则
- `public_tunnel.py`：公网隧道启动与管理
- `templates/`：页面模板
- `static/`：样式与前端交互
