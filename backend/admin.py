"""后台管理模块 · 直接维护前台比赛系统的真实数据。

设计要点：
  * 后台与前台共用 .local/match.db 和同一组表：
      - 比赛管理读写 matches（名称 / 时间 / 状态），比分从 score_games 实时聚合，只读展示；
      - 账号管理读写 players / teachers 的真实资料、角色与启用状态。
    比分是 961 条计分流水的结果，后台不提供直接改分，避免出现「流水与比分对不上」。
  * 后台有独立的登录会话（admin_id）与 CSRF（admin_csrf），不与前台姓名+验证码体系混用。
  * 每次后台写操作都会递增 meta.revision，前台在线页面会立即看到变化。
  * 默认管理员：admin / admin123（werkzeug 加盐哈希），首次启动写入。
"""
from __future__ import annotations

import math
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import db

admin = Blueprint("admin", __name__, url_prefix="/admin")

ADMIN_SESSION_KEY = "admin_id"
ADMIN_CSRF_KEY = "admin_csrf"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin123"

STATUS_CHOICES = ["未开始", "进行中", "已结束", "已取消"]
ROLE_CHOICES = ["队长", "队员", "老师", "观众"]
TEAM_CHOICES = ["A", "B", "C", "D"]
GENDER_CHOICES = ["男", "女"]
PER_PAGE = 10

NAME_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9_·]{1,20}$")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 表结构迁移与初始化
# ---------------------------------------------------------------------------

def _seed_match_metadata(conn) -> None:
    """给已有的真实比赛补上名称、时间与状态；只在未初始化时执行。"""
    rows = conn.execute(
        "SELECT id, round_no, team_a, team_b, name FROM matches ORDER BY id"
    ).fetchall()
    pending = [r for r in rows if not (r["name"] or "").strip()]
    if not pending:
        return
    for row in pending:
        mid = row["id"]
        earliest = conn.execute(
            "SELECT min(started_at) FROM score_games WHERE match_id = ?", (mid,)
        ).fetchone()[0]
        match_time = ""
        if earliest:
            try:
                moment = datetime.fromisoformat(earliest)
                if moment.tzinfo:
                    moment = moment.astimezone(timezone(timedelta(hours=8)))
                match_time = moment.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                match_time = str(earliest)[:16]
        total = conn.execute(
            "SELECT count(*) FROM score_games WHERE match_id = ?", (mid,)
        ).fetchone()[0]
        done = conn.execute(
            "SELECT count(*) FROM score_games WHERE match_id = ? AND state = 'finished'",
            (mid,),
        ).fetchone()[0]
        status = "已结束" if done >= 6 else ("进行中" if total else "未开始")
        conn.execute(
            "UPDATE matches SET name = ?, match_time = ?, status = ? WHERE id = ?",
            (
                f"第{row['round_no']}轮 · {row['team_a']}组 vs {row['team_b']}组",
                match_time,
                status,
                mid,
            ),
        )


def init_admin_db() -> None:
    """建后台管理员表，并把共享表升级到含后台字段的版本（可重复调用）。"""
    db.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    with db.connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admin_users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name  TEXT NOT NULL DEFAULT '',
                enabled       INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL
            );
            """
        )
        _seed_match_metadata(conn)
        if not conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone():
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, display_name, enabled, created_at)"
                " VALUES (?, ?, ?, 1, ?)",
                (DEFAULT_USERNAME, generate_password_hash(DEFAULT_PASSWORD), "系统管理员", _now()),
            )


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _csrf_token() -> str:
    return session.setdefault(ADMIN_CSRF_KEY, secrets.token_urlsafe(24))


def _valid_csrf() -> bool:
    expected = session.get(ADMIN_CSRF_KEY)
    return bool(expected) and secrets.compare_digest(
        request.form.get("csrf", ""), expected
    )


def _current_admin():
    admin_id = session.get(ADMIN_SESSION_KEY)
    if not admin_id:
        return None
    with db.connection() as conn:
        row = conn.execute(
            "SELECT id, username, display_name FROM admin_users"
            " WHERE id = ? AND enabled = 1",
            (admin_id,),
        ).fetchone()
    return dict(row) if row else None


def _query_one(sql: str, params=()):
    with db.connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _slice(rows: list, page: int):
    total = len(rows)
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(max(1, page), pages)
    return rows[(page - 1) * PER_PAGE: page * PER_PAGE], total, pages, page


def _page_arg() -> int:
    try:
        return int(request.args.get("page", "1"))
    except (TypeError, ValueError):
        return 1


def _match_rows() -> list[dict]:
    """把真实比赛 + 真实比分聚合成后台表格需要的行。"""
    outcomes = db.match_results()
    rows = []
    for match in db.all_matches():
        outcome = outcomes.get(match["id"], {})
        real = {
            "pending": "未开始", "running": "进行中", "finished": "已结束",
        }.get(outcome.get("state"), "未开始")
        name = (match.get("name") or "").strip() or (
            f"第{match['round_no']}轮 · {match['team_a']}组 vs {match['team_b']}组"
        )
        rows.append({
            "id": match["id"],
            "name": name,
            "match_time": match.get("match_time") or "",
            "participants": f"{match['team_a']}组 vs {match['team_b']}组",
            "team_a": match["team_a"],
            "team_b": match["team_b"],
            "round_no": match["round_no"],
            "court": match["court"],
            "score": outcome.get("text") or "0:0",
            "real_state": real,
            "status": (match.get("status") or "").strip() or real,
            "finished": bool(outcome.get("finished")),
            "net": outcome.get("net"),
            "done_games": outcome.get("done_games", 0),
        })
    return rows


def _account_rows() -> list[dict]:
    """把真实队员与老师合并成统一账号列表。"""
    rows = []
    with db.connection() as conn:
        for player in conn.execute("SELECT * FROM players ORDER BY team, sort_order"):
            rows.append({
                "key": f"p{player['id']}",
                "source": "player",
                "id": player["id"],
                "username": player["name"],
                "display_name": player["name"],
                "role": "队长" if player["is_captain"] else "队员",
                "team": player["team"],
                "gender": player["gender"],
                "is_captain": bool(player["is_captain"]),
                "sort_order": player["sort_order"],
                "enabled": bool(player["enabled"]),
                "email": "",
                "phone": "",
            })
        for teacher in conn.execute("SELECT * FROM teachers ORDER BY sort_order"):
            rows.append({
                "key": f"t{teacher['id']}",
                "source": "teacher",
                "id": teacher["id"],
                "username": teacher["name"],
                "display_name": teacher["name"],
                "role": "老师",
                "team": "",
                "gender": "",
                "is_captain": False,
                "sort_order": teacher["sort_order"],
                "enabled": bool(teacher["enabled"]),
                "email": "",
                "phone": "",
            })
    return rows


@admin.before_request
def authenticate_admin():
    if request.endpoint == "admin.login":
        return None
    current = _current_admin()
    if current is None:
        session.pop(ADMIN_SESSION_KEY, None)
        return redirect(url_for("admin.login", next=request.path), code=303)
    g.admin_user = current
    return None


@admin.context_processor
def inject_admin_context():
    return {
        "admin_csrf": _csrf_token(),
        "admin_user": getattr(g, "admin_user", None),
        "status_choices": STATUS_CHOICES,
        "role_choices": ROLE_CHOICES,
        "team_choices": TEAM_CHOICES,
        "gender_choices": GENDER_CHOICES,
    }


@admin.app_errorhandler(404)
def admin_not_found(_error):
    if not request.path.startswith("/admin/"):
        return render_template("error.html", message="页面不存在"), 404
    return render_template("admin/not_found.html"), 404


# ---------------------------------------------------------------------------
# 登录 / 退出 / 首页
# ---------------------------------------------------------------------------

@admin.route("/login", methods=["GET", "POST"])
def login():
    if session.get(ADMIN_SESSION_KEY) and _current_admin():
        return redirect(url_for("admin.dashboard"), code=303)
    error = None
    if request.method == "POST":
        if not _valid_csrf():
            abort(403)
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with db.connection() as conn:
            row = conn.execute(
                "SELECT id, password_hash, enabled FROM admin_users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None or not row["enabled"] or not check_password_hash(row["password_hash"], password):
            error = "账号或密码不正确，或该账号已被禁用"
        else:
            session.clear()
            session[ADMIN_SESSION_KEY] = row["id"]
            session[ADMIN_CSRF_KEY] = secrets.token_urlsafe(24)
            flash("登录成功，欢迎进入后台管理", "ok")
            target = request.form.get("next", "")
            if not target.startswith("/admin/") or target.startswith("//"):
                target = url_for("admin.dashboard")
            return redirect(target, code=303)
    return render_template("admin/login.html", error=error), (401 if error else 200)


@admin.post("/logout")
def logout():
    if not _valid_csrf():
        abort(403)
    session.clear()
    flash("已安全退出后台管理", "ok")
    return redirect(url_for("admin.login"), code=303)


@admin.get("/")
def dashboard():
    rows = _match_rows()
    accounts = _account_rows()
    with db.connection() as conn:
        revision = conn.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
    return render_template(
        "admin/dashboard.html",
        comp_total=len(rows),
        comp_running=sum(1 for r in rows if r["status"] == "进行中"),
        acc_total=len(accounts),
        acc_disabled=sum(1 for a in accounts if not a["enabled"]),
        recent=rows[:5],
        revision=revision,
    )


# ---------------------------------------------------------------------------
# 比赛数据管理（直接读写 matches）
# ---------------------------------------------------------------------------

@admin.get("/competitions")
def competitions():
    q = request.args.get("q", "").strip().lower()
    status = request.args.get("status", "").strip()
    rows = _match_rows()
    if q:
        rows = [
            r for r in rows
            if q in (r["name"] + r["participants"] + str(r["score"])).lower()
        ]
    if status in STATUS_CHOICES:
        rows = [r for r in rows if r["status"] == status]
    rows.sort(key=lambda r: (r["round_no"], r["court"]))
    page_rows, total, pages, page = _slice(rows, _page_arg())
    return render_template(
        "admin/competitions.html",
        rows=page_rows, total=total, pages=pages, page=page,
        q=q, status=status,
    )


def _read_match_form(exclude_id=None):
    fields = ("name", "match_time", "status", "team_a", "team_b", "round_no", "court")
    data = {f: str(request.form.get(f, "")).strip() for f in fields}
    if not data["name"] or len(data["name"]) > 120:
        return data, "比赛名称不能为空且不超过 120 字"
    if data["status"] not in STATUS_CHOICES:
        return data, "比赛状态无效"
    if data["team_a"] not in TEAM_CHOICES or data["team_b"] not in TEAM_CHOICES:
        return data, "参赛组别无效"
    if data["team_a"] == data["team_b"]:
        return data, "对阵双方不能是同一组"
    try:
        data["round_no"] = int(data["round_no"])
        data["court"] = int(data["court"])
    except (TypeError, ValueError):
        return data, "轮次与场地必须是数字"
    if data["round_no"] < 1 or data["court"] < 1:
        return data, "轮次与场地必须为正整数"
    with db.connection() as conn:
        dup = conn.execute(
            "SELECT 1 FROM matches WHERE round_no = ? AND court = ? AND id != ?",
            (data["round_no"], data["court"], exclude_id if exclude_id else -1),
        ).fetchone()
    if dup:
        return data, f"第{data['round_no']}轮 · 场地{data['court']} 已有比赛"
    data["match_time"] = data["match_time"][:100]
    return data, None


@admin.route("/competitions/new", methods=["GET", "POST"])
def competition_new():
    data = {
        "name": "", "match_time": "", "status": "未开始",
        "team_a": "A", "team_b": "B", "round_no": 1, "court": 1,
    }
    if request.method == "POST":
        if not _valid_csrf():
            abort(403)
        data, error = _read_match_form()
        if error:
            flash(error, "error")
        else:
            with db.connection() as conn:
                conn.execute(
                    "INSERT INTO matches"
                    " (round_no, court, team_a, team_b, name, match_time, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (data["round_no"], data["court"], data["team_a"], data["team_b"],
                     data["name"], data["match_time"], data["status"]),
                )
            flash(f"比赛「{data['name']}」已新增，前台赛程将自动刷新", "ok")
            return redirect(url_for("admin.competitions"), code=303)
    return render_template("admin/competition_form.html", data=data, mode="new", item=None)


@admin.route("/competitions/<int:cid>/edit", methods=["GET", "POST"])
def competition_edit(cid: int):
    rows = {r["id"]: r for r in _match_rows()}
    if cid not in rows:
        abort(404)
    data = dict(rows[cid])
    if request.method == "POST":
        if not _valid_csrf():
            abort(403)
        data, error = _read_match_form(exclude_id=cid)
        if error:
            flash(error, "error")
        else:
            with db.connection() as conn:
                conn.execute(
                    "UPDATE matches SET round_no=?, court=?, team_a=?, team_b=?,"
                    " name=?, match_time=?, status=? WHERE id=?",
                    (data["round_no"], data["court"], data["team_a"], data["team_b"],
                     data["name"], data["match_time"], data["status"], cid),
                )
            flash(f"比赛「{data['name']}」已保存，前台赛程将自动刷新", "ok")
            return redirect(url_for("admin.competitions"), code=303)
    return render_template("admin/competition_form.html", data=data, mode="edit", item=data)


@admin.post("/competitions/<int:cid>/delete")
def competition_delete(cid: int):
    if not _valid_csrf():
        abort(403)
    item = _query_one("SELECT name FROM matches WHERE id = ?", (cid,))
    if item is None:
        abort(404)
    with db.connection() as conn:
        refs = [r[0] for r in conn.execute(
            "SELECT ref FROM score_games WHERE match_id = ?", (cid,)
        )]
        for ref in refs:
            conn.execute("DELETE FROM score_events WHERE ref = ?", (ref,))
            conn.execute("DELETE FROM guesses WHERE kind = 'game' AND ref = ?", (ref,))
        conn.execute("DELETE FROM score_games WHERE match_id = ?", (cid,))
        conn.execute("DELETE FROM match_lineups WHERE match_id = ?", (cid,))
        conn.execute("DELETE FROM lineups WHERE match_id = ?", (cid,))
        conn.execute("DELETE FROM matches WHERE id = ?", (cid,))
    flash(f"比赛「{item['name']}」及其计分记录已删除", "ok")
    return redirect(url_for("admin.competitions"), code=303)


# ---------------------------------------------------------------------------
# 账号管理（直接读写 players / teachers）
# ---------------------------------------------------------------------------

def _find_account(key: str):
    match = re.fullmatch(r"([pt])(\d+)", key or "")
    if not match:
        return None
    source = "player" if match.group(1) == "p" else "teacher"
    table = "players" if source == "player" else "teachers"
    row = _query_one(f"SELECT * FROM {table} WHERE id = ?", (int(match.group(2)),))
    if not row:
        return None
    row["source"] = source
    row["key"] = key
    row["username"] = row["name"]
    row["role"] = ("队长" if row["is_captain"] else "队员") if source == "player" else "老师"
    row["enabled"] = bool(row["enabled"])
    return row


@admin.get("/accounts")
def accounts():
    q = request.args.get("q", "").strip().lower()
    role = request.args.get("role", "").strip()
    enabled = request.args.get("enabled", "").strip()
    rows = _account_rows()
    if q:
        rows = [r for r in rows if q in (r["username"] + r["team"]).lower()]
    if role in ROLE_CHOICES:
        rows = [r for r in rows if r["role"] == role]
    if enabled in ("1", "0"):
        rows = [r for r in rows if r["enabled"] == (enabled == "1")]
    rows.sort(key=lambda r: (r["source"] != "player", r["team"], r["sort_order"]))
    page_rows, total, pages, page = _slice(rows, _page_arg())
    return render_template(
        "admin/accounts.html",
        rows=page_rows, total=total, pages=pages, page=page,
        q=q, role=role, enabled=enabled,
    )


def _read_account_form(exclude_key=None):
    data = {
        "source": request.form.get("source", "").strip(),
        "username": request.form.get("username", "").strip(),
        "role": request.form.get("role", "").strip(),
        "team": request.form.get("team", "").strip(),
        "gender": request.form.get("gender", "").strip(),
        "sort_order": request.form.get("sort_order", "0").strip(),
        "enabled": bool(request.form.get("enabled")),
    }
    if data["source"] not in ("player", "teacher"):
        return data, "账号类型无效"
    if data["source"] == "teacher":
        data["role"] = "老师"
        data["team"] = ""
        data["gender"] = ""
    if not NAME_RE.match(data["username"]):
        return data, "姓名需为 1-20 个中文字、字母、数字或下划线"
    if data["role"] not in ("队长", "队员", "老师"):
        return data, "角色无效"
    if data["source"] == "player" and data["role"] == "老师":
        return data, "队员账号的角色只能是队长或队员"
    if data["source"] == "player":
        if data["team"] not in TEAM_CHOICES:
            return data, "参赛组别无效"
        if data["gender"] not in GENDER_CHOICES:
            return data, "性别无效"
    try:
        data["sort_order"] = int(data["sort_order"] or 0)
    except (TypeError, ValueError):
        return data, "排序值必须是数字"
    with db.connection() as conn:
        for table in ("players", "teachers"):
            sql = f"SELECT 1 FROM {table} WHERE name = ?"
            params = [data["username"]]
            if exclude_key:
                current = _find_account(exclude_key)
                if current and current["source"] == ("player" if table == "players" else "teacher"):
                    sql += " AND id != ?"
                    params.append(current["id"])
            if conn.execute(sql, params).fetchone():
                return data, "该姓名已存在于队员或老师名单中"
    return data, None


@admin.route("/accounts/new", methods=["GET", "POST"])
def account_new():
    data = {
        "source": "player", "username": "", "role": "队员", "team": "A",
        "gender": "男", "sort_order": 0, "enabled": True,
    }
    if request.method == "POST":
        if not _valid_csrf():
            abort(403)
        data, error = _read_account_form()
        if error:
            flash(error, "error")
        else:
            with db.connection() as conn:
                if data["source"] == "player":
                    conn.execute(
                        "INSERT INTO players (name, team, gender, is_captain, sort_order, enabled)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (data["username"], data["team"], data["gender"],
                         1 if data["role"] == "队长" else 0,
                         data["sort_order"], int(data["enabled"])),
                    )
                else:
                    conn.execute(
                        "INSERT INTO teachers (name, sort_order, enabled) VALUES (?, ?, ?)",
                        (data["username"], data["sort_order"], int(data["enabled"])),
                    )
            flash(f"账号「{data['username']}」已加入真实名单，前台立即生效", "ok")
            return redirect(url_for("admin.accounts"), code=303)
    return render_template("admin/account_form.html", data=data, mode="new", item=None)


@admin.route("/accounts/<key>/edit", methods=["GET", "POST"])
def account_edit(key: str):
    item = _find_account(key)
    if item is None:
        abort(404)
    data = {
        "source": item["source"], "username": item["username"], "role": item["role"],
        "team": item.get("team") or "A", "gender": item.get("gender") or "男",
        "sort_order": item.get("sort_order") or 0, "enabled": item["enabled"],
    }
    if request.method == "POST":
        if not _valid_csrf():
            abort(403)
        data, error = _read_account_form(exclude_key=key)
        if error:
            flash(error, "error")
        else:
            table = "players" if item["source"] == "player" else "teachers"
            with db.connection() as conn:
                if item["source"] == "player":
                    conn.execute(
                        "UPDATE players SET name=?, team=?, gender=?, is_captain=?,"
                        " sort_order=?, enabled=? WHERE id=?",
                        (data["username"], data["team"], data["gender"],
                         1 if data["role"] == "队长" else 0,
                         data["sort_order"], int(data["enabled"]), item["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE teachers SET name=?, sort_order=?, enabled=? WHERE id=?",
                        (data["username"], data["sort_order"], int(data["enabled"]), item["id"]),
                    )
            flash(f"账号「{data['username']}」已保存，前台立即生效", "ok")
            return redirect(url_for("admin.accounts"), code=303)
    return render_template("admin/account_form.html", data=data, mode="edit", item=item)


@admin.post("/accounts/<key>/toggle")
def account_toggle(key: str):
    if not _valid_csrf():
        abort(403)
    item = _find_account(key)
    if item is None:
        abort(404)
    table = "players" if item["source"] == "player" else "teachers"
    new_value = 0 if item["enabled"] else 1
    with db.connection() as conn:
        conn.execute(f"UPDATE {table} SET enabled = ? WHERE id = ?", (new_value, item["id"]))
    flash(f"账号「{item['username']}」已{'启用' if new_value else '禁用'}", "ok")
    return redirect(url_for("admin.accounts"), code=303)


@admin.post("/accounts/<key>/delete")
def account_delete(key: str):
    if not _valid_csrf():
        abort(403)
    item = _find_account(key)
    if item is None:
        abort(404)
    table = "players" if item["source"] == "player" else "teachers"
    with db.connection() as conn:
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (item["id"],))
    flash(f"账号「{item['username']}」已从真实名单中删除", "ok")
    return redirect(url_for("admin.accounts"), code=303)
