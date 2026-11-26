"""比赛管理系统 · 唯一后端入口。

架构参考 Where to charge：Flask + SQLite + Jinja2 + 原生前端，无构建工具。
整套系统只运行一个 Flask 进程、只读写 .local/match.db：
  * 本机 / 局域网：双击 start.cmd，浏览器访问 127.0.0.1:8768
  * 公网：双击 start-public.cmd，由 Cloudflare / ngrok 隧道转发回本进程
因此本机、局域网、公网看到的是同一份数据，不存在两份数据库需要同步。
请勿同时启动第二个 app.py 实例；start-public.cmd 会复用已运行的服务。

登录规则（按需求定制，不使用密码与注册）：
  1. 先选身份：我是选手/观众 / 我是老师
  2. 老师：从 D老师、L老师、Z老师三选一（不要求验证码）
  3. 选手/观众：输入真实姓名 + 该姓名自己的 4 位验证码；名单内为参赛选手
     （含赛中临时加入本组名单的人），名单外作为观众放行
  4. 参赛选手显示「你是 X 组的选手」，队长显示「你是 X 组的队长」，观众显示「你是观众」
  5. 每个姓名 = 一个独立账号，验证码初始都是 1234；登录后只能改自己的码，
     改码只让「该账号」的旧会话失效，不影响其他姓名及其会话
  6. 队长可在 /team 管理本组成员（替换队员、删除队员），其他身份只读
  7. 每组只有一份唯一的 6 局安排，放在首页：队员/队长只看本组，观众与老师看不到
  8. 只有本组队长能调整安排并点击「发布」；老师只读；已发布需先撤回才能再改
  9. /schedule 仅在四组全部发布后显示对阵（只显示「A VS B」+ 按组筛选，
     不显示轮次、场地、安排）；未全部发布时只显示发布进度
"""
from __future__ import annotations

import os
import re
import secrets
import threading
import time
from datetime import timedelta
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import db
import admin as admin_module

# 所有路径都相对本文件推导：backend/ 的上一级即项目根目录，换机器/换盘符都可用。
ROOT = Path(__file__).resolve().parent.parent
SECRET_PATH = ROOT / ".local" / "secret_key.txt"
# 云端发布平台会注入 PORT，并要求绑定 0.0.0.0；本地默认仍是 8768
PORT = int(os.environ.get("PORT", "8768"))
HOST = os.environ.get("HOST", "0.0.0.0")


def load_secret() -> str:
    """签名密钥落盘，服务重启后已登录用户不需要重新登录。"""
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_PATH.exists():
        value = SECRET_PATH.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_hex(32)
    SECRET_PATH.write_text(value, encoding="utf-8")
    return value


# 模板与静态资源都在相邻的 frontend/ 目录；Flask 按模块所在目录（backend/）
# 解析这两个相对路径，因此不需要写任何机器相关的绝对路径。
app = Flask(
    __name__,
    template_folder="../frontend/templates",
    static_folder="../frontend/static",
    static_url_path="/static",  # 浏览器里的 /static/... URL 保持不变
)
app.config.update(
    SESSION_COOKIE_NAME="match_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,  # 内部 HTTP 访问；若上 HTTPS 改为 True
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=16 * 1024,
    TEMPLATES_AUTO_RELOAD=True,  # 改模板后立即生效，内部调整界面无需重启
)
app.secret_key = load_secret()

db.init_db()
admin_module.init_admin_db()
# 后台管理独立挂在 /admin，使用自己的账号与会话，不共用前台身份体系。
app.register_blueprint(admin_module.admin)

# ---------- 验证码失败限流（4 位数字只有一万种组合，必须挡住暴力尝试） ----------
# 按「访客 + 姓名」分桶：试错某个账号不会连带锁死其他人。
_FAIL_LOCK = threading.Lock()
_PASSCODE_FAILS: dict[str, list] = {}
FAIL_LIMIT = 5
FAIL_WINDOW = 60.0


def client_key() -> str:
    """隧道转发时优先用 X-Forwarded-For 区分不同访客。"""
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() if forwarded else "") or (
        request.remote_addr or "?"
    )


def pin_fail_key(name: str) -> str:
    return f"{client_key()}|{name}"


def passcode_block_seconds(key: str) -> int:
    """仍在冷却期返回剩余秒数，否则返回 0。"""
    now = time.monotonic()
    with _FAIL_LOCK:
        entry = _PASSCODE_FAILS.get(key)
        if not entry:
            return 0
        count, first = entry
        if now - first >= FAIL_WINDOW:
            _PASSCODE_FAILS.pop(key, None)
            return 0
        if count < FAIL_LIMIT:
            return 0
        return max(1, int(FAIL_WINDOW - (now - first)) + 1)


def note_passcode_failure(key: str) -> None:
    now = time.monotonic()
    with _FAIL_LOCK:
        entry = _PASSCODE_FAILS.get(key)
        if not entry or now - entry[1] >= FAIL_WINDOW:
            _PASSCODE_FAILS[key] = [1, now]
        else:
            entry[0] += 1


def clear_passcode_failures(key: str) -> None:
    with _FAIL_LOCK:
        _PASSCODE_FAILS.pop(key, None)


def current_identity():
    """每次请求回查数据库：名单变化、本人验证码变化都能立即生效。"""
    role = session.get("role")
    name = session.get("name")
    if role not in ("student", "teacher") or not name:
        return None
    if role == "teacher":
        if not db.is_teacher(name):
            return None
        return {"role": "teacher", "name": name, "team": None, "is_captain": False}
    if session.get("pinv") != db.pin_version(name):
        # 只有「这个姓名」改过验证码才作废旧会话；其他账号、其他页面不受影响
        session.clear()
        return None
    player = db.find_player(name)
    if player and not player.get("enabled", True):
        session.clear()
        return None
    if not player:
        # 非参赛选手同样允许进入，身份标记为观众；每次请求回查，
        # 之后名单新增此人会自动转为队员，队员移出名单会自动转为观众。
        return {
            "role": "student",
            "name": name,
            "team": None,
            "is_captain": False,
            "gender": None,
            "is_spectator": True,
        }
    return {
        "role": "student",
        "name": name,
        "team": player["team"],
        "is_captain": bool(player["is_captain"]),
        "gender": player["gender"],
        "is_spectator": False,
    }


@app.before_request
def guard():
    g.current = current_identity()
    # 后台 /admin 用自己的登录会话与 CSRF，且比赛结束后仍可维护数据，
    # 因此完全跳过前台的「未登录跳登录」与「终局锁」逻辑。
    if request.endpoint in ("login", "static") or (request.endpoint or "").startswith("admin."):
        return None
    if not g.current:
        return redirect(url_for("login"), code=303)
    if request.method == "POST" and request.endpoint not in ("login", "logout", "change_passcode"):
        # 与计分共用可重入锁，避免第四次确认和晚到的记分请求互相穿插。
        db._score_lock.acquire()
        g.tournament_write_lock = True
        if request.endpoint not in ("tournament_confirm", "tournament_dismiss") and db.tournament_is_finalized():
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify(ok=False, error="比赛已结束，最终榜单已锁定"), 409
            flash("比赛已结束，最终榜单已锁定", "error")
            return redirect(url_for("standings_page"), code=303)
    return None


@app.teardown_request
def release_tournament_write_lock(error=None):
    if g.pop("tournament_write_lock", False):
        db._score_lock.release()


@app.after_request
def secure(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def form_csrf() -> str:
    """管理页表单用的 CSRF token，与登录页 token 分开。"""
    return session.setdefault("form_csrf", secrets.token_urlsafe(24))


def valid_form_csrf() -> bool:
    expected = session.get("form_csrf")
    return bool(expected) and secrets.compare_digest(
        request.form.get("csrf", ""), expected
    )


def require_captain():
    """只有本组队长能进入成员管理，其余身份一律 403。"""
    identity = g.current
    if identity["role"] != "student" or not identity["is_captain"]:
        abort(403)
    return identity


def can_edit_team(identity, team: str) -> bool:
    """只有本组队长可编辑；老师、队员、观众一律只读。"""
    return bool(
        identity["role"] == "student"
        and identity.get("is_captain")
        and identity["team"] == team
    )


@app.errorhandler(403)
def forbidden(_error):
    if request.path == "/passcode":
        message = "只有选手账号可以修改本人的登录验证码。"
    elif request.path.startswith("/lineup/match"):
        message = "只有本组队长可以调整未开赛小局的人员。"
    elif request.path.startswith("/lineup"):
        message = "只有本组队长可以调整与发布安排。"
    elif request.path.startswith("/referee"):
        message = "只有参赛选手可以进入裁判台并记分。"
    elif request.path.startswith("/admin/"):
        message = "后台操作校验失败（表单已过期），请刷新页面后重试。"
    else:
        message = "只有本组队长可以管理成员名单。"
    return render_template("error.html", message=message), 403


def render_login(error=None, status=200, name_value="", initial="role", csrf=None):
    token = csrf or session.setdefault("login_csrf", secrets.token_urlsafe(24))
    return (
        render_template(
            "login.html",
            csrf=token,
            error=error,
            name_value=name_value,
            initial_step=initial,
        ),
        status,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.get("current"):
        return redirect(url_for("home"), code=303)
    csrf = session.setdefault("login_csrf", secrets.token_urlsafe(24))
    if request.method == "GET":
        return render_login(csrf=csrf)

    if not secrets.compare_digest(request.form.get("csrf", ""), csrf):
        return render_login("页面已失效，请重新打开登录页", 403, csrf=csrf)

    role = request.form.get("role", "")

    if role == "teacher":
        teacher = request.form.get("teacher", "").strip()
        if not db.is_teacher(teacher):
            db.log_login("teacher", teacher or "(空)", None, "invalid_teacher")
            return render_login("请选择有效的老师身份", 400, initial="teacher", csrf=csrf)
        session.clear()
        session["role"] = "teacher"
        session["name"] = teacher
        session.permanent = True
        db.log_login("teacher", teacher, None, "ok")
        return redirect(url_for("home"), code=303)

    if role == "student":
        name = request.form.get("name", "").strip()
        if not name or len(name) > 20:
            db.log_login("student", name or "(空)", None, "invalid_name")
            return render_login(
                "请输入 1–20 个字符的真实姓名",
                400,
                name_value=name,
                initial="student",
                csrf=csrf,
            )
        code = request.form.get("passcode", "").strip()
        key = pin_fail_key(name)
        blocked = passcode_block_seconds(key)
        if blocked:
            db.log_login("student", name, None, "passcode_throttled")
            return render_login(
                f"尝试次数过多，请 {blocked} 秒后再试",
                429,
                name_value=name,
                initial="student",
                csrf=csrf,
            )
        if not re.fullmatch(r"\d{4}", code) or not db.verify_student_pin(name, code):
            db.log_login("student", name, None, "bad_passcode")
            note_passcode_failure(key)
            return render_login(
                "验证码不正确",
                401,
                name_value=name,
                initial="student",
                csrf=csrf,
            )
        clear_passcode_failures(key)
        player = db.find_player(name)
        if player and not player.get("enabled", True):
            db.log_login("student", name, player["team"], "disabled")
            return render_login(
                "该账号已被管理员禁用，请联系管理员",
                403,
                name_value=name,
                initial="student",
                csrf=csrf,
            )
        session.clear()
        session["role"] = "student"
        session["name"] = player["name"] if player else name
        session["pinv"] = db.pin_version(session["name"])
        session.permanent = True
        db.log_login(
            "student",
            session["name"],
            player["team"] if player else None,
            "ok" if player else "ok_spectator",
        )
        return redirect(url_for("home"), code=303)

    db.log_login("unknown", "", None, "invalid_role")
    return render_login("请先选择身份", 400, csrf=csrf)


@app.get("/")
def home():
    identity = g.current
    is_student = identity["role"] == "student"
    # 只有参赛队员/队长能看到本组成员与本组安排；观众、老师看不到安排
    member_view = is_student and not identity["is_spectator"]
    team = identity["team"] if member_view else None
    members = db.team_members(team) if member_view else None
    lineup = db.team_lineup(team) if member_view else []
    status = db.publication_status().get(team) if member_view else None
    published = bool(status and status["published"])
    return render_template(
        "home.html",
        identity=identity,
        members=members,
        teams=None if member_view else db.all_teams(),
        pin_custom=db.pin_is_custom(identity["name"]) if is_student else False,
        csrf=form_csrf(),
        show_lineup=member_view,
        lineup=lineup,
        lineup_map={row["game_no"]: row for row in lineup},
        roster_names={m["name"] for m in members} if member_view else set(),
        publication=status,
        lineup_editable=member_view
        and can_edit_team(identity, team)
        and not published,
        games=db.GAMES,
    )


@app.post("/passcode")
def change_passcode():
    """凭本人当前验证码，只修改「这个姓名」自己的验证码。"""
    if not valid_form_csrf():
        abort(403)
    identity = g.current
    if identity["role"] != "student":
        abort(403)
    name = identity["name"]
    key = pin_fail_key(name)
    blocked = passcode_block_seconds(key)
    old = request.form.get("old_passcode", "").strip()
    new = request.form.get("new_passcode", "").strip()
    confirm = request.form.get("confirm_passcode", "").strip()
    if blocked:
        flash(f"尝试次数过多，请 {blocked} 秒后再试", "error")
    elif not re.fullmatch(r"\d{4}", old) or not db.verify_student_pin(name, old):
        note_passcode_failure(key)
        flash("当前验证码不正确", "error")
    elif not re.fullmatch(r"\d{4}", new):
        flash("新验证码必须是 4 位数字", "error")
    elif new != confirm:
        flash("两次输入的新验证码不一致", "error")
    elif new == old:
        flash("新验证码不能与当前验证码相同", "error")
    else:
        version = db.set_student_pin(name, new, name)
        session["pinv"] = version
        clear_passcode_failures(key)
        flash(
            f"「{name}」的登录验证码已更新，只影响这个账号；"
            "其他已登录的姓名不受影响",
            "ok",
        )
    return redirect(url_for("home"), code=303)


@app.get("/api/revision")
def api_revision():
    """前端轮询此接口判断数据是否变动；版本号由 SQLite 触发器维护。"""
    return jsonify(revision=db.current_revision())


@app.context_processor
def inject_live_status():
    """所有含导航的页面共享：是否有进行中比赛（决定「查看赛程」的提示点）。"""
    return {"has_live": bool(g.get("current")) and db.has_running_game(),
            "champion_csrf": form_csrf() if g.get("current") else ""}


STATIC_ROOT = ROOT / "frontend" / "static"


@app.context_processor
def inject_asset_version():
    """给所有 /static/ 引用加版本号，避免浏览器 / CDN 用旧 CSS、旧 JS。

    取静态目录中最新的 mtime（毫秒），任一文件改动 URL 就会变；每次请求
    重新计算，所以只改静态文件、不重启后端也能立即生效。
    """
    try:
        latest = max(
            (p.stat().st_mtime_ns // 1_000_000
             for p in STATIC_ROOT.rglob("*") if p.is_file()),
            default=0,
        )
    except OSError:
        latest = 0
    return {"asset_v": str(latest)}


@app.get("/api/live/status")
def api_live_status():
    """轻量状态轮询：只返回是否有进行中比赛，不返回比分明细、不刷新页面。"""
    return jsonify(live=db.has_running_game())


# 卡片配色按项目区分：混双 / 男双 / 男单
EVENT_CLASS = {"混双": "mixed", "男双": "double", "男单": "single"}


@app.get("/schedule")
def schedule():
    """赛程展示：四组全部发布后显示所有对局；有小组撤回发布时，
    只保留已经开赛（进行中 / 已结束）的局，未开赛的局一律不显示。

    开赛局的阵容在开赛时已固化，撤回发布不会泄露未对局信息；
    筛选（组别 / 类别 / 姓名搜索）在浏览器端即时完成，不改变 URL 以外任何状态。
    """
    status = db.publication_status()
    published_count = sum(1 for item in status.values() if item["published"])
    all_published = published_count > 0 and published_count == len(status)
    events = list(dict.fromkeys(game["event"] for game in db.GAMES))
    identity = g.current
    is_captain = bool(
        identity["role"] == "student" and identity.get("is_captain") and identity["team"]
    )
    team = identity.get("team") if is_captain else None
    csrf = form_csrf()

    # 已开赛局（进行中 / 已结束）的 ref 集合：撤回发布后只保留这些局。
    started_refs = set(db.all_score_games())
    lineups = db.all_effective_lineups()
    # 队长仅能改「本组参与、且该小局尚未开赛」的人员；已开赛局一律只读。
    editable_refs: set[str] = set()
    if team:
        for match in db.all_matches():
            if team not in (match["team_a"], match["team_b"]):
                continue
            for game in db.GAMES:
                ref = "%d-%d" % (match["id"], game["no"])
                if ref not in started_refs:
                    editable_refs.add(ref)
    schedule_matches = []
    for match in db.all_matches():
        games = []
        for game in db.GAMES:
            ref = "%d-%d" % (match["id"], game["no"])
            # 有小组未发布时，未开赛的局不显示；已开赛的局照常显示。
            if not all_published and ref not in started_refs:
                continue
            row_a = lineups[(match["id"], match["team_a"])].get(game["no"], {})
            row_b = lineups[(match["id"], match["team_b"])].get(game["no"], {})
            games.append(
                {
                    "no": game["no"],
                    "event": game["event"],
                    "event_key": EVENT_CLASS.get(game["event"], "mixed"),
                    "players_a": [
                        name
                        for name in (row_a.get("player1"), row_a.get("player2"))
                        if name
                    ],
                    "players_b": [
                        name
                        for name in (row_b.get("player1"), row_b.get("player2"))
                        if name
                    ],
                    "lineup_a": row_a,
                    "lineup_b": row_b,
                }
            )
        if not games:
            continue
        schedule_matches.append(
            {
                "match_id": match["id"],
                "round_no": match["round_no"],
                "court": match["court"],
                "team_a": match["team_a"],
                "team_b": match["team_b"],
                "games": games,
            }
        )
    return render_template(
        "schedule.html",
        identity=identity,
        pending=False,
        partial=not all_published,
        status=status,
        published_count=published_count,
        schedule_matches=schedule_matches,
        events=events,
        total_games=sum(len(item["games"]) for item in schedule_matches),
        csrf=csrf,
        members_map=db.all_teams() if team else {},
        editable_refs=editable_refs,
        custom_matches=db.custom_matches_all(),
    )


# ----------------------------- 比赛记分 · 追分版 -----------------------------

@app.get("/guess")
def guess_page():
    """比赛竞猜页。

    赛程未公布前只开放「冠军组别竞猜」；四组全部发布后，额外开放「小局竞猜」，
    并在该区块展示赛程对阵卡片与每局的竞猜选项。
    """
    identity = g.current
    status = db.publication_status()
    published = db.all_published()
    teams = list(db.ROSTER.keys())
    user = db.get_user_guesses(identity["name"])
    schedule_matches = []
    if published:
        lineups = db.all_effective_lineups()
        for match in db.all_matches():
            games = []
            for game in db.GAMES:
                row_a = lineups.get((match["id"], match["team_a"]), {}).get(game["no"], {})
                row_b = lineups.get((match["id"], match["team_b"]), {}).get(game["no"], {})
                games.append(
                    {
                        "no": game["no"],
                        "event": game["event"],
                        "event_key": EVENT_CLASS.get(game["event"], "mixed"),
                        "players_a": [
                            n for n in (row_a.get("player1"), row_a.get("player2")) if n
                        ],
                        "players_b": [
                            n for n in (row_b.get("player1"), row_b.get("player2")) if n
                        ],
                    }
                )
            schedule_matches.append(
                {
                    "match_id": match["id"],
                    "team_a": match["team_a"],
                    "team_b": match["team_b"],
                    "games": games,
                }
            )
    return render_template(
        "guess.html",
        identity=identity,
        published=published,
        status=status,
        teams=teams,
        champion_pick=user["champion"],
        game_picks=user["games"],
        schedule_matches=schedule_matches,
        locked_refs=set(db.all_score_games()),
        champion_locked=db.has_started_game(),
        csrf=form_csrf(),
    )


@app.post("/guess/champion")
def guess_champion():
    """提交 / 修改冠军组别竞猜；每人限一条，覆盖即更新。

    第一小局开赛后即锁定，不能再选择或修改（含已提交过的用户）。
    前端用 fetch 静默提交（带 X-Requested-With 头），此时返回 JSON，
    页面不刷新、不弹窗，仅由前端就地高亮；保留普通表单提交作为兜底。
    """
    identity = g.current
    if not valid_form_csrf():
        abort(403)
    ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if db.has_started_game():
        message = "第一小局比赛已开始，冠军组别竞猜已锁定"
        if ajax:
            return jsonify(ok=False, error=message, locked=True), 403
        flash(message, "error")
        return redirect(url_for("guess_page"), code=303)
    pick = (request.form.get("pick") or "").strip()
    if pick not in db.ROSTER:
        if ajax:
            return jsonify(ok=False, error="请选择有效的冠军组别"), 400
        flash("请选择有效的冠军组别", "error")
        return redirect(url_for("guess_page"), code=303)
    if not db.save_guess(
        identity["name"], "champion", "champion", pick, guesser_stake(identity)
    ):
        return jsonify(ok=False, error="冠军竞猜已锁定", locked=True), 403
    if ajax:
        return jsonify(ok=True, kind="champion", pick=pick)
    flash(f"已竞猜 {pick} 组夺冠，可随时修改", "ok")
    return redirect(url_for("guess_page"), code=303)


@app.post("/guess/game")
def guess_game():
    """提交 / 修改某一小局率先获得十五分的竞猜；仅赛程公布后开放。

    与冠军竞猜一致：前端用 fetch 静默提交时返回 JSON，页面不刷新。
    """
    identity = g.current
    if not valid_form_csrf():
        abort(403)
    ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not db.all_published():
        if ajax:
            return jsonify(ok=False, error="赛程尚未公布，小局竞猜暂未开放"), 403
        flash("赛程尚未公布，小局竞猜暂未开放", "error")
        return redirect(url_for("guess_page"), code=303)
    ref = (request.form.get("ref") or "").strip()
    pick = (request.form.get("pick") or "").strip()
    try:
        match_id, game_no = (int(x) for x in ref.split("-"))
    except (ValueError, AttributeError):
        if ajax:
            return jsonify(ok=False, error="竞猜参数无效"), 400
        flash("竞猜参数无效", "error")
        return redirect(url_for("guess_page"), code=303)
    ref = db.match_ref(match_id, game_no)
    match = next((m for m in db.all_matches() if m["id"] == match_id), None)
    valid_games = {g["no"] for g in db.GAMES}
    if not match or game_no not in valid_games:
        if ajax:
            return jsonify(ok=False, error="竞猜参数无效"), 400
        flash("竞猜参数无效", "error")
    elif pick not in (match["team_a"], match["team_b"]):
        if ajax:
            return jsonify(ok=False, error="竞猜选项不属于本局对阵双方"), 400
        flash("竞猜选项不属于本局对阵双方", "error")
    elif db.score_game_state(ref) != "idle":
        message = "本局比赛已开始，竞猜已锁定"
        if ajax:
            return jsonify(ok=False, error=message), 403
        flash(message, "error")
    else:
        if not db.save_guess(
            identity["name"], "game", ref, pick, guesser_stake(identity)
        ):
            return jsonify(ok=False, error="本局竞猜已锁定", locked=True), 403
        if ajax:
            return jsonify(ok=True, kind="game", ref=ref, pick=pick, game_no=game_no)
        flash(f"第 {game_no} 局已竞猜 {pick} 组本局先获十五分，可随时修改", "ok")
    return redirect(url_for("guess_page"), code=303)


@app.post("/guess/game/cancel")
def guess_game_cancel():
    """取消某一小局率先获得十五分的竞猜（撤回选择）。"""
    identity = g.current
    if not valid_form_csrf():
        abort(403)
    ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not db.all_published():
        if ajax:
            return jsonify(ok=False, error="赛程尚未公布，小局竞猜暂未开放"), 403
        return redirect(url_for("guess_page"), code=303)
    ref = (request.form.get("ref") or "").strip()
    try:
        match_id, game_no = (int(x) for x in ref.split("-"))
    except (ValueError, AttributeError):
        if ajax:
            return jsonify(ok=False, error="竞猜参数无效"), 400
        return redirect(url_for("guess_page"), code=303)
    ref = db.match_ref(match_id, game_no)
    match = next((m for m in db.all_matches() if m["id"] == match_id), None)
    if not match or game_no not in {g["no"] for g in db.GAMES}:
        if ajax:
            return jsonify(ok=False, error="竞猜参数无效"), 400
        return redirect(url_for("guess_page"), code=303)
    if db.score_game_state(ref) != "idle":
        if ajax:
            return jsonify(ok=False, error="本局比赛已开始，竞猜已锁定"), 403
        return redirect(url_for("guess_page"), code=303)
    if not db.delete_guess(identity["name"], "game", ref):
        return jsonify(ok=False, error="本局竞猜已锁定", locked=True), 403
    if ajax:
        return jsonify(ok=True, kind="game", ref=ref, game_no=game_no)
    return redirect(url_for("guess_page"), code=303)


# ----------------------------- 比赛记分 -----------------------------
def guesser_stake(identity) -> int:
    """下注分数（= 竞猜计分公式的「基础分数」）：学生 / 观众 1，老师 2。"""
    return 2 if identity["role"] == "teacher" else 1


@app.get("/api/guess")
def api_guess():
    response = jsonify(games=db.guess_results(g.current["name"]),
                       champion_locked=db.has_started_game(),
                       standings=db.standings()["guesses"])
    response.headers["Cache-Control"] = "no-store"
    return response


def parse_ref(ref: str):
    """把 'matchId-gameNo' 解析成 (match, game_no)；非法返回 (None, None)。"""
    try:
        parts = (ref or "").split("-")
        if len(parts) != 2:
            return None, None
        match_id, game_no = int(parts[0]), int(parts[1])
    except (ValueError, AttributeError):
        return None, None
    if game_no not in {g["no"] for g in db.GAMES}:
        return None, None
    match = next((m for m in db.all_matches() if m["id"] == match_id), None)
    return match, game_no


def require_referee():
    """只有参赛选手能担任裁判；观众与老师一律 403。"""
    identity = g.current
    if identity["role"] != "student" or identity.get("is_spectator"):
        abort(403)
    return identity


def referee_groups():
    """裁判台所需的 36 局列表（含各自记分状态与开赛裁判）。"""
    # 裁判台看到的是实际阵容：已开赛局为开赛时固化的名单，未开赛局含队长的场次级调整。
    lineups = db.all_effective_lineups()
    scores = db.all_score_games()
    groups = []
    for match in db.all_matches():
        games = []
        carry_a, carry_b = 0, 0
        for game in db.GAMES:
            ref = f'{match["id"]}-{game["no"]}'
            row_a = lineups[(match["id"], match["team_a"])].get(game["no"], {})
            row_b = lineups[(match["id"], match["team_b"])].get(game["no"], {})
            s = scores.get(ref)
            start_a = s["start_a"] if s else carry_a
            start_b = s["start_b"] if s else carry_b
            games.append({
                "no": game["no"], "event": game["event"], "ref": ref,
                "players_a": [n for n in (row_a.get("player1"), row_a.get("player2")) if n],
                "players_b": [n for n in (row_b.get("player1"), row_b.get("player2")) if n],
                "state": s["state"] if s else "idle",
                "score_a": s["score_a"] if s else carry_a,
                "score_b": s["score_b"] if s else carry_b,
                "referee": s["referee"] if s else "",
                "court_input": s["court_input"] if s else "",
                "switched": s["switched"] if s else 0,
                "start_a": start_a,
                "start_b": start_b,
                "target": db.game_rule(game["no"])["target"],
                "switch_at": db.game_rule(game["no"])["switch_at"],
            })
            if s and s["state"] == "finished":
                carry_a, carry_b = s["score_a"], s["score_b"]
        # 顺序锁：与 db.start_score_game 同口径——第 N 局（N>1）须上一局已结束，
        # 且本场次没有正在进行的局，才允许显示场地序号与确认开始。
        any_running = any(g["state"] == "running" for g in games)
        running_no = next((g["no"] for g in games if g["state"] == "running"), 0)
        for idx, g in enumerate(games):
            prev_no = g["no"] - 1 if g["no"] > 1 else 0
            prev_finished = prev_no == 0 or games[idx - 1]["state"] == "finished"
            g["prev_no"] = prev_no
            g["can_start"] = bool(
                g["state"] == "idle" and prev_finished and not any_running
            )
            g["lock_reason"] = (
                ("本场次第 %d 局进行中" % running_no) if any_running
                else ("请先结束第 %d 局" % prev_no)
            ) if (g["state"] == "idle" and not g["can_start"]) else ""
        groups.append({
            "match_id": match["id"], "round_no": match["round_no"],
            "court": match["court"], "team_a": match["team_a"],
            "team_b": match["team_b"], "games": games,
        })
    return groups


def _scoring_ref(ref: str):
    """记分写操作的共同校验：局次合法、进行中、且是当前裁判开的赛。"""
    match, _ = parse_ref(ref)
    if not match:
        return None, None, "无效的局次"
    row = db.score_game(ref)
    if not row or row["state"] != "running":
        return None, None, "这一局不在进行中"
    if row["referee"] != g.current["name"]:
        return None, None, f'这一局由 {row["referee"]} 裁判记分'
    return match, row, None


def _custom_active(cm: dict, identity: dict):
    """把一条自定义对局构造成与正式局同形状的 active，供 _scoring_panel.html 复用。

    自定义模式不设换边、不从上一局承接，新增 is_custom / custom_id / win_by_two 字段，
    前端据此切换记分接口并隐藏「换边」提示与弹窗。
    """
    running = cm["state"] == "running"
    return {
        "ref": "c%d" % cm["id"], "custom_id": cm["id"], "is_custom": True,
        "no": 0, "event": cm["event"],
        "round_no": 0, "court": "自定义",
        "team_a": cm["team_a"], "team_b": cm["team_b"],
        "players_a": cm.get("players_a_list", []),
        "players_b": cm.get("players_b_list", []),
        "state": cm["state"], "score_a": cm["score_a"], "score_b": cm["score_b"],
        "winner": (cm.get("winner") or "") if cm["state"] == "finished" else "",
        "target": cm["target"],
        "win_by_two": bool(cm.get("win_by_two")),
        "start_a": 0, "start_b": 0,
        "switch_at": 0, "switched": 0,
        "can_score": running, "held_by_other": False,
        # 比分达标但尚未确认时，winner 暂存候选胜方；弹窗据此恢复，榜单仍未计入。
        "need_switch": False,
        "reached_end": bool(running and (cm.get("winner") or "")),
        "pending_winner": cm.get("winner") or "",
        "serve": None,  # 自定义对局不设换边 / 发球提示
        "can_start": False, "lock_reason": "",
        "referee": cm["referee"], "court_input": "自定义",
    }


@app.get("/referee")
def referee_page():
    """裁判台：选局 + 场地序号 + 开赛 + 记分。

    ?ref=1-3 显式进入某一局（原有流程）；不带参数时若本人有正在执裁的局，
    自动恢复该局记分界面并置顶，保留比分、换边以及待确认的判定进度。
    """
    identity = require_referee()
    custom_matches = db.custom_matches_all()
    custom_ctx = {
        "custom_matches": custom_matches,
        "custom_events": db.CUSTOM_EVENTS,
        "custom_teams": db.all_teams(),
    }
    want_custom = (request.args.get("custom") or "").strip()
    want_ref = (request.args.get("ref") or "").strip()
    # 自定义对局独立于正式发布状态：显式 ?custom= 时直接用统一记分面板打开，
    # 不因四组未全部发布而被挡在进度页。
    if want_custom.isdigit():
        cm = next((x for x in custom_matches if x["id"] == int(want_custom)), None)
        if cm:
            return render_template(
                "referee.html", identity=identity, pending=False,
                groups=[], active=_custom_active(cm, identity), resumed=False,
                csrf=form_csrf(), **custom_ctx,
            )
    # 重新进入裁判台自动恢复本人进行中的对局：无显式 ref/custom 时，
    # 若本人有进行中的自定义对局且没有进行中的正式局，直接进自定义记分卡片。
    # 自定义对局不受发布状态影响，因此放在 all_published 判断之前。
    if not want_ref and not want_custom:
        own_custom = next(
            (x for x in custom_matches
             if x["state"] == "running" and x["referee"] == identity["name"]),
            None,
        )
        own_formal = db.running_ref_for(identity["name"]) if db.all_published() else None
        if own_custom and not own_formal:
            return render_template(
                "referee.html", identity=identity, pending=False,
                groups=[], active=_custom_active(own_custom, identity), resumed=True,
                csrf=form_csrf(), **custom_ctx,
            )
    all_pub = db.all_published()
    groups = referee_groups()
    resumed = False
    if not want_ref:
        # 撤回发布后仍要自动恢复本人正在执裁的局，不能中断已开始的记分进程。
        own = db.running_ref_for(identity["name"])
        if own:
            want_ref, resumed = own, True
    active = None
    if want_ref:
        for m in groups:
            for g in m["games"]:
                if g["ref"] != want_ref:
                    continue
                running = g["state"] == "running"
                mine = g["referee"] == identity["name"]
                active = {
                    **g,
                    "round_no": m["round_no"], "court": m["court"],
                    "team_a": m["team_a"], "team_b": m["team_b"],
                    "can_score": running and mine,
                    "held_by_other": running and not mine,
                    # 重新进入时恢复尚未确认的换边 / 结束判定
                    "need_switch": bool(running and not g["switched"]
                                        and max(g["score_a"], g["score_b"]) >= g["switch_at"]),
                    "reached_end": bool(running
                                        and max(g["score_a"], g["score_b"]) >= g["target"]),
                    "serve": db.serve_hint(g["ref"], m["team_a"], m["team_b"]) if running else None,
                }
                break
            if active:
                break
    # 撤回发布只隐藏未开赛局：本人正在执裁的局必须继续保留记分卡片。
    # 记分写接口本身不校验发布状态，因此进行中的比分不会被撤回动作影响。
    if not all_pub:
        mine_running = bool(
            active and active["state"] == "running"
            and active["referee"] == identity["name"]
        )
        if not mine_running:
            return render_template(
                "referee.html", identity=identity, pending=True,
                groups=[], active=None, resumed=False, csrf=form_csrf(),
                **custom_ctx,
            )
        # 只保留进行中的本人局，列表不再展示未发布场次的其他局，避免泄露未对局信息。
        groups = []
    return render_template(
        "referee.html", identity=identity, pending=False,
        groups=groups, active=active,
        resumed=bool(resumed and active), csrf=form_csrf(),
        **custom_ctx,
    )


@app.post("/referee/start")
def referee_start():
    identity = require_referee()
    if not valid_form_csrf():
        return jsonify(ok=False, error="页面已失效，请刷新"), 403
    ref = (request.form.get("ref") or "").strip()
    match, game_no = parse_ref(ref)
    if not match:
        return jsonify(ok=False, error="无效的局次"), 400
    court = (request.form.get("court") or "").strip()[:20]
    if not court:
        return jsonify(ok=False, error="请填写场地序号"), 400
    ok, message = db.start_score_game(
        ref, match["id"], game_no, court, identity["name"]
    )
    return jsonify(ok=ok, error="" if ok else message, ref=ref)


@app.post("/referee/score")
def referee_score():
    require_referee()
    if not valid_form_csrf():
        return jsonify(ok=False, error="页面已失效，请刷新"), 403
    ref = (request.form.get("ref") or "").strip()
    match, _, err = _scoring_ref(ref)
    if err:
        return jsonify(ok=False, error=err), 400
    team = (request.form.get("team") or "").strip()
    return jsonify(**db.add_point(ref, team, match["team_a"], match["team_b"]))


@app.post("/referee/undo")
def referee_undo():
    require_referee()
    if not valid_form_csrf():
        return jsonify(ok=False, error="页面已失效，请刷新"), 403
    ref = (request.form.get("ref") or "").strip()
    _, _, err = _scoring_ref(ref)
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(**db.undo_point(ref))


@app.post("/referee/switch")
def referee_switch():
    """裁判在 8 分弹窗里确认「已完成对调」：只标记一次。"""
    require_referee()
    if not valid_form_csrf():
        return jsonify(ok=False, error="页面已失效，请刷新"), 403
    ref = (request.form.get("ref") or "").strip()
    _, _, err = _scoring_ref(ref)
    if err:
        return jsonify(ok=False, error=err), 400
    db.mark_switched(ref)
    return jsonify(ok=True)


@app.post("/referee/finish")
def referee_finish():
    require_referee()
    if not valid_form_csrf():
        return jsonify(ok=False, error="页面已失效，请刷新"), 403
    ref = (request.form.get("ref") or "").strip()
    match, _, err = _scoring_ref(ref)
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(**db.finish_score_game(ref))


# ----------------------------- 自定义对局 -----------------------------
@app.post("/referee/custom/create")
def referee_custom_create():
    """裁判台底部发起自定义对局：两组 + 项目 + 上场人员 + 目标分 + 连胜两分。"""
    identity = require_referee()
    if not valid_form_csrf():
        return jsonify(ok=False, error="页面已失效，请刷新"), 403
    team_a = (request.form.get("team_a") or "").strip()
    team_b = (request.form.get("team_b") or "").strip()
    event = (request.form.get("event") or "").strip()
    pa = [v.strip() for v in request.form.getlist("players_a") if v.strip()]
    pb = [v.strip() for v in request.form.getlist("players_b") if v.strip()]
    try:
        target = int((request.form.get("target") or "").strip())
    except ValueError:
        target = 0
    win_by_two = request.form.get("win_by_two") in ("1", "true", "on", "yes")
    match_id, message = db.create_custom_match(
        team_a, team_b, event, pa, pb, target, win_by_two, identity["name"]
    )
    if match_id is None:
        return jsonify(ok=False, error=message), 400
    return jsonify(ok=True, id=match_id, message=message)


def _custom_match_owned(referee: str):
    """自定义对局的写权限：任何参赛选手都能记分，但只能删自己创建的。"""
    match_id = request.form.get("id", type=int)
    if not match_id:
        return None, None, "无效的对局"
    return match_id, True, None


@app.post("/referee/custom/point")
def referee_custom_point():
    identity = require_referee()
    if not valid_form_csrf():
        return jsonify(ok=False, error="页面已失效，请刷新"), 403
    match_id, _, err = _custom_match_owned(identity["name"])
    if err:
        return jsonify(ok=False, error=err), 400
    team = (request.form.get("team") or "").strip()
    return jsonify(**db.custom_add_point(match_id, team))


@app.post("/referee/custom/undo")
def referee_custom_undo():
    require_referee()
    if not valid_form_csrf():
        return jsonify(ok=False, error="页面已失效，请刷新"), 403
    match_id, _, err = _custom_match_owned(None)
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(**db.custom_undo_point(match_id))


@app.post("/referee/custom/finish")
def referee_custom_finish():
    """自定义对局二次确认：确认后才结束并把胜场/净胜分计入榜单。"""
    identity = require_referee()
    if not valid_form_csrf():
        return jsonify(ok=False, error="页面已失效，请刷新"), 403
    match_id, _, err = _custom_match_owned(identity["name"])
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(**db.custom_finish(match_id))


@app.post("/referee/custom/delete")
def referee_custom_delete():
    identity = require_referee()
    if not valid_form_csrf():
        return jsonify(ok=False, error="页面已失效，请刷新"), 403
    match_id, _, err = _custom_match_owned(identity["name"])
    if err:
        return jsonify(ok=False, error=err), 400
    with db.connection() as conn:
        row = conn.execute(
            "SELECT referee FROM custom_matches WHERE id = ?", (match_id,)
        ).fetchone()
    if not row:
        return jsonify(ok=False, error="对局不存在"), 400
    if row["referee"] != identity["name"]:
        return jsonify(ok=False, error="只能删除自己创建的对局"), 403
    db.custom_delete(match_id)
    return jsonify(ok=True)


@app.get("/api/live")
def api_live():
    """赛程页增量刷新数据源：局明细按 v 比对局部更新，另附场次完赛比分与自定义对局。"""
    return jsonify(
        items=db.live_payload(g.current["name"]),
        matches=db.match_results(),
        custom=db.custom_live_payload(),
    )


@app.get("/standings")
def standings_page():
    return render_template(
        "standings.html", identity=g.current,
        data=db.standings(), tournament=db.tournament_status(), csrf=form_csrf(),
    )


@app.get("/api/tournament")
def api_tournament():
    return jsonify(db.tournament_status(g.current["name"]))


@app.post("/tournament/dismiss")
def tournament_dismiss():
    if not valid_form_csrf():
        abort(403)
    ok = db.dismiss_champion_celebration(g.current["name"], request.form.get("finalized_at", ""))
    return jsonify(ok=ok), (200 if ok else 409)


@app.post("/tournament/confirm")
def tournament_confirm():
    identity = require_captain()
    if not valid_form_csrf():
        abort(403)
    result = db.confirm_tournament_end(identity["team"], identity["name"])
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(result), (200 if result["ok"] else 409)
    flash("比赛已结束，冠军竞猜已结算" if result.get("finalized") else
          ("本组已确认，等待其他队长" if result["ok"] else result["error"]),
          "ok" if result["ok"] else "error")
    return redirect(url_for("standings_page"), code=303)


def lineup_form_rows():
    rows = []
    for game in db.GAMES:
        no = game["no"]
        rows.append(
            {
                "game_no": no,
                "event": game["event"],
                "player1": request.form.get(f"p1_{no}", ""),
                "player2": request.form.get(f"p2_{no}", "")
                if game["size"] == 2
                else "",
            }
        )
    return rows


@app.post("/lineup/save")
def lineup_save():
    identity = require_captain()
    if not valid_form_csrf():
        abort(403)
    ok, message, warnings = db.save_team_lineup(
        identity["team"], identity["name"], lineup_form_rows()
    )
    flash(message, "ok" if ok else "error")
    for warning in warnings[:6]:
        flash(warning, "warn")
    return redirect(url_for("home") + "#lineup", code=303)


@app.post("/lineup/reset")
def lineup_reset():
    identity = require_captain()
    if not valid_form_csrf():
        abort(403)
    ok, message, warnings = db.reset_team_lineup(
        identity["team"], identity["name"]
    )
    flash(message, "ok" if ok else "error")
    for warning in warnings[:6]:
        flash(warning, "warn")
    return redirect(url_for("home") + "#lineup", code=303)


@app.post("/lineup/publish")
def lineup_publish():
    identity = require_captain()
    if not valid_form_csrf():
        abort(403)
    publish = request.form.get("action") == "publish"
    _, message = db.publish_lineup(identity["team"], identity["name"], publish)
    flash(message, "ok")
    return redirect(url_for("home") + "#lineup", code=303)


def _match_edit_target():
    """场次级改人的共同校验：队长本人、本组参与该场，返回 (match_id, game_no) 或 None。"""
    identity = require_captain()
    if not valid_form_csrf():
        abort(403)
    try:
        match_id = int((request.form.get("match_id") or "").strip())
        game_no = int((request.form.get("game_no") or "").strip())
    except ValueError:
        return None
    match = next((m for m in db.all_matches() if m["id"] == match_id), None)
    if not match or identity["team"] not in (match["team_a"], match["team_b"]):
        abort(403)
    return match_id, game_no, identity


@app.post("/lineup/match-save")
def lineup_match_save():
    """开赛后队长仅改本组在「未开赛小局」的上场人员，按场次隔离。"""
    target = _match_edit_target()
    if not target:
        flash("无效的场次或局次", "error")
        return redirect(url_for("schedule"), code=303)
    match_id, game_no, identity = target
    ok, message = db.save_match_lineup(
        match_id, identity["team"], identity["name"], game_no,
        request.form.get("player1", ""), request.form.get("player2", ""),
        new_name=request.form.get("new_player", ""),
        new_gender=request.form.get("new_gender", ""),
        new_slot=request.form.get("new_slot", "1"),
    )
    flash(message, "ok" if ok else "error")
    return redirect(url_for("schedule") + "#m%dg%d" % (match_id, game_no), code=303)


@app.post("/lineup/match-reset")
def lineup_match_reset():
    """该场该局恢复默认安排；已开赛局会被数据层拒绝。"""
    target = _match_edit_target()
    if not target:
        flash("无效的场次或局次", "error")
        return redirect(url_for("schedule"), code=303)
    match_id, game_no, identity = target
    ok, message = db.reset_match_lineup(match_id, identity["team"], identity["name"], game_no)
    flash(message, "ok" if ok else "error")
    return redirect(url_for("schedule") + "#m%dg%d" % (match_id, game_no), code=303)


@app.get("/team")
def team_manage():
    identity = require_captain()
    return render_template(
        "team.html",
        identity=identity,
        members=db.team_members(identity["team"]),
        changes=db.recent_changes(identity["team"]),
        csrf=form_csrf(),
    )


@app.post("/team/replace")
def team_replace():
    identity = require_captain()
    if not valid_form_csrf():
        abort(403)
    target = request.form.get("target", "").strip()
    new_name = request.form.get("new_name", "").strip()
    gender = request.form.get("gender", "")
    if not new_name or len(new_name) > 20:
        flash("新队员姓名需为 1–20 个字符", "error")
    elif new_name == target:
        flash("新姓名与原姓名相同", "error")
    else:
        ok, message = db.replace_player(
            identity["team"], identity["name"], target, new_name, gender
        )
        flash(message, "ok" if ok else "error")
    return redirect(url_for("team_manage"), code=303)


@app.post("/team/delete")
def team_delete():
    identity = require_captain()
    if not valid_form_csrf():
        abort(403)
    target = request.form.get("target", "").strip()
    ok, message = db.delete_player(identity["team"], identity["name"], target)
    flash(message, "ok" if ok else "error")
    return redirect(url_for("team_manage"), code=303)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"), code=303)


if __name__ == "__main__":
    print(f"Match management system: http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False, debug=False)
