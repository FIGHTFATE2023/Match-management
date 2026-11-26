"""比赛管理系统 · SQLite 存储层。

参考 Where to charge 的做法：数据放在项目内 .local/match.db，不依赖云数据库。
本机与公网（隧道）共用同一个后端进程，因此全程只需维护这一份数据库文件；
名单、安排、发布状态与各账号验证码的写入都会由 SQLite 触发器递增 meta.revision，
前端据此自动刷新。

安排口径（2026-11-12 改）：每组只有一份唯一的 6 局出场安排（team_lineups），
不随对手/场次变化；队长在首页调整并「发布」，四组全部发布后赛程页才公布对阵。
旧的 lineups（按场次 72 行）保留作历史，运行时不再读写。

ROSTER 只是「初始名单」：首次启动时写入一次，之后名单以数据库为准，
这样队长在系统内做的替换 / 删除不会被重启时的同步覆盖。

账号模型：每个姓名就是一个独立账号，各有自己的 4 位登录验证码，
初始都是 1234；student_pins 只保存「改过码」的账号（PBKDF2 哈希 + 版本号），
没记录的账号按初始值校验，因此登录本身不会写库、不会触发全站刷新。
改码只影响该账号，不会改变别人的验证码，也不会踢掉别人的会话。
如需重置某个账号：db.reset_student_pin("姓名")；重置全部初始名单则删库重启。
"""
from __future__ import annotations

import json
import hashlib
import hmac
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# backend/ 的上一级就是项目根目录，.local/ 与前后端目录同级
ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = ROOT / ".local"
DB_PATH = LOCAL_DIR / "match.db"

DEFAULT_PIN = "1234"
_PBKDF2_ROUNDS = 120_000

# 初始名单：来自「分组名单」截图，并由用户确认了女生与队长
ROSTER = {
    "A": ["王n", "张gy", "肖nd", "许f", "肖yu", "王wz"],
    "B": ["许go", "邹s", "陶d", "王ts", "占m", "马gw"],
    "C": ["马ut", "刘jk", "李if", "潘sg", "李yv", "邓ef"],
    "D": ["丁u", "曹d", "何za", "卢tw", "王cs", "董fp", "孔sh"],
}
FEMALES = {"王wz", "马gw", "李yv", "邓ef", "董fp", "孔sh"}
CAPTAINS = {"王n": "A", "许go": "B", "马ut": "C", "丁u": "D"}
TEACHERS = ["D老师", "L老师", "Z老师"]

# 单场 6 局：局次、项目、上场人数。局次与项目固定，队长只调整上场人员。
GAMES = [
    {"no": 1, "event": "混双", "size": 2},
    {"no": 2, "event": "男双", "size": 2},
    {"no": 3, "event": "男单", "size": 1},
    {"no": 4, "event": "男双", "size": 2},
    {"no": 5, "event": "混双", "size": 2},
    {"no": 6, "event": "男单", "size": 1},
]
# 自定义对局可选的项目类型（裁判台底部手动发起，独立于正式 6 局）。
CUSTOM_EVENTS = [
    {"event": "混双", "size": 2},
    {"event": "男双", "size": 2},
    {"event": "男单", "size": 1},
    {"event": "女单", "size": 1},
]

# 4 组单循环：3 轮 × 每轮 2 场并行，每组恰好 3 场且对手互不相同。
MATCH_PLAN = [
    {"id": 1, "round": 1, "court": 1, "teams": ("A", "B")},
    {"id": 2, "round": 1, "court": 2, "teams": ("C", "D")},
    {"id": 3, "round": 2, "court": 1, "teams": ("A", "C")},
    {"id": 4, "round": 2, "court": 2, "teams": ("B", "D")},
    {"id": 5, "round": 3, "court": 1, "teams": ("A", "D")},
    {"id": 6, "round": 3, "court": 2, "teams": ("B", "C")},
]


@contextmanager
def connection():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _hash_passcode(code: str, salt: bytes | None = None) -> str:
    """PBKDF2 + 随机盐，避免数据库文件里出现明文验证码。"""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def _check_passcode(code: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", code.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _migrate_legacy_passcode(conn):
    """旧版是「全局共用一个码」，把它迁移成「改码人自己的 PIN」。

    其余姓名（含观众）回到初始 1234。迁移只做一次，用 meta 标记；
    不知道新码明文也没关系，直接搬运 PBKDF2 哈希即可。
    """
    if conn.execute("SELECT 1 FROM meta WHERE key = 'pin_v2_migrated'").fetchone():
        return
    legacy = conn.execute(
        "SELECT value FROM settings WHERE key = 'student_passcode_hash'"
    ).fetchone()
    changed = conn.execute(
        "SELECT identity FROM login_logs WHERE outcome = 'passcode_changed' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if legacy and changed:
        name = (changed["identity"] or "").strip()
        if name and name != "(system)":
            conn.execute(
                "INSERT OR IGNORE INTO student_pins "
                "(name, pass_hash, pin_version, updated_at) VALUES (?, ?, 2, ?)",
                (name, legacy["value"], _now()),
            )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('pin_v2_migrated', '1')"
    )


def init_db() -> None:
    """建表；仅在首次启动时写入 ROSTER 与赛程，之后一切以数据库为准。"""
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    with connection() as db:
        db.executescript(
            """
            -- 清除已废弃的触发器：旧全局验证码、旧按场次安排
            DROP TRIGGER IF EXISTS settings_rev_insert;
            DROP TRIGGER IF EXISTS settings_rev_update;
            DROP TRIGGER IF EXISTS settings_rev_delete;
            DROP TRIGGER IF EXISTS lineups_rev_insert;
            DROP TRIGGER IF EXISTS lineups_rev_update;
            DROP TRIGGER IF EXISTS lineups_rev_delete;

            CREATE TABLE IF NOT EXISTS players (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                team        TEXT NOT NULL,
                gender      TEXT NOT NULL,
                is_captain  INTEGER NOT NULL DEFAULT 0,
                sort_order  INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS teachers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                sort_order  INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS login_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at  TEXT NOT NULL,
                role         TEXT NOT NULL,
                identity     TEXT NOT NULL,
                team         TEXT,
                outcome      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL
            );
            -- 旧版全局验证码存这里，仅为一次性迁移保留，运行时不再读写
            CREATE TABLE IF NOT EXISTS settings (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL
            );
            -- 每个姓名就是一个独立账号；这里只保存「改过验证码」的账号。
            -- 没有记录的账号按 DEFAULT_PIN 校验，因此登录本身不写库、不触发刷新。
            CREATE TABLE IF NOT EXISTS student_pins (
                name         TEXT PRIMARY KEY,
                pass_hash    TEXT NOT NULL,
                pin_version  INTEGER NOT NULL DEFAULT 1,
                updated_at   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS roster_changes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at  TEXT NOT NULL,
                operator     TEXT NOT NULL,
                team         TEXT NOT NULL,
                action       TEXT NOT NULL,
                old_name     TEXT NOT NULL,
                new_name     TEXT,
                gender       TEXT
            );
            CREATE TABLE IF NOT EXISTS matches (
                id        INTEGER PRIMARY KEY,
                round_no  INTEGER NOT NULL,
                court     INTEGER NOT NULL,
                team_a    TEXT NOT NULL,
                team_b    TEXT NOT NULL,
                UNIQUE (round_no, court)
            );
            -- 旧版按场次存 72 行，仅保留作迁移来源与历史，不再读写
            CREATE TABLE IF NOT EXISTS lineups (
                match_id  INTEGER NOT NULL,
                team      TEXT NOT NULL,
                game_no   INTEGER NOT NULL,
                event     TEXT NOT NULL,
                player1   TEXT NOT NULL,
                player2   TEXT,
                PRIMARY KEY (match_id, team, game_no)
            );
            -- 每组唯一一份 6 局出场安排，不随对手变化
            CREATE TABLE IF NOT EXISTS team_lineups (
                team      TEXT NOT NULL,
                game_no   INTEGER NOT NULL,
                event     TEXT NOT NULL,
                player1   TEXT NOT NULL,
                player2   TEXT,
                PRIMARY KEY (team, game_no)
            );
            -- 场次级人员覆盖：开赛后队长只能改「未开赛小局」的本组上场人员。
            -- 开赛瞬间会把当时的实际人员固化到这里，已开赛局因此永远显示当时的阵容。
            -- 未开赛局若没有覆盖行，则回退到 team_lineups 的默认安排。
            CREATE TABLE IF NOT EXISTS match_lineups (
                match_id  INTEGER NOT NULL,
                team      TEXT NOT NULL,
                game_no   INTEGER NOT NULL,
                event     TEXT NOT NULL,
                player1   TEXT NOT NULL,
                player2   TEXT,
                PRIMARY KEY (match_id, team, game_no)
            );
            CREATE INDEX IF NOT EXISTS idx_match_lineups_match ON match_lineups(match_id, team);
            -- 队长确认后发布；四组都发布，赛程页才公布对阵
            CREATE TABLE IF NOT EXISTS lineup_publications (
                team          TEXT PRIMARY KEY,
                published_at  TEXT NOT NULL,
                operator      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_players_team ON players(team, sort_order);
            CREATE INDEX IF NOT EXISTS idx_lineups_team ON lineups(team, match_id);


            -- 追分记分：一个场次的 6 局共用累计比分，每局一条主记录。
            -- start_a / start_b 是本局继承来的起始比分；score_a / score_b 是当前
            -- 累计比分；target / switch_at 是本局的结束目标分与换边提示分。
            -- winner 列仅为兼容旧库保留；追分版不按小局记录胜方，
            -- 场次胜场由逐分流水推导（整场先到 90 分者），不落库。
            -- 比分永远按赛程的 team_a / team_b 绝对组别存储，换边只改界面位置。
            -- 竞猜选择独立保存，不触发名单/赛程的全站刷新。
            -- game 按本局增量先到十五分判定；champion 按最终小组榜结算。
            CREATE TABLE IF NOT EXISTS tournament_confirmations (
                team TEXT PRIMARY KEY, captain TEXT NOT NULL,
                confirmed_at TEXT NOT NULL, fingerprint TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tournament_result (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                finalized_at TEXT NOT NULL, snapshot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tournament_champion_dismissals (
                name TEXT NOT NULL, finalized_at TEXT NOT NULL, dismissed_at TEXT NOT NULL,
                PRIMARY KEY (name, finalized_at)
            );
            CREATE TABLE IF NOT EXISTS guesses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                kind        TEXT NOT NULL,
                ref         TEXT NOT NULL,
                pick        TEXT NOT NULL,
                stake       INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                UNIQUE (name, kind, ref)
            );
            CREATE INDEX IF NOT EXISTS idx_guesses_name ON guesses(name);

            CREATE TABLE IF NOT EXISTS score_games (
                ref          TEXT PRIMARY KEY,
                match_id     INTEGER NOT NULL,
                game_no      INTEGER NOT NULL,
                court_input  TEXT NOT NULL DEFAULT '',
                referee      TEXT NOT NULL,
                state        TEXT NOT NULL DEFAULT 'running',
                start_a      INTEGER NOT NULL DEFAULT 0,
                start_b      INTEGER NOT NULL DEFAULT 0,
                score_a      INTEGER NOT NULL DEFAULT 0,
                score_b      INTEGER NOT NULL DEFAULT 0,
                target       INTEGER NOT NULL DEFAULT 15,
                switch_at    INTEGER NOT NULL DEFAULT 8,
                winner       TEXT,
                switched     INTEGER NOT NULL DEFAULT 0,
                rev          INTEGER NOT NULL DEFAULT 0,
                started_at   TEXT NOT NULL,
                finished_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS score_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ref           TEXT NOT NULL,
                seq           INTEGER NOT NULL,
                scoring_team  TEXT NOT NULL,
                score_a       INTEGER NOT NULL,
                score_b       INTEGER NOT NULL,
                created_at    TEXT NOT NULL,
                UNIQUE (ref, seq)
            );
            CREATE INDEX IF NOT EXISTS idx_score_events_ref ON score_events(ref, seq);
            -- 注意：记分表刻意不注册 meta.revision 触发器。
            -- 比分每几秒变化一次，若走 revision 会导致全站页面反复整页刷新；
            -- 赛程页改用 /api/live 做按 ref 的增量局部更新。

            -- 自定义对局：裁判台底部手动发起，独立于 6 场正式循环赛。
            -- 组别名次不绑定 match_id，比分从 0 开始，可设目标分与「连胜两分」。
            -- 结束后算额外小组胜场 + 净胜分，并按上场人员计入选手积分。
            CREATE TABLE IF NOT EXISTS custom_matches (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                team_a        TEXT NOT NULL,
                team_b        TEXT NOT NULL,
                event         TEXT NOT NULL,
                players_a     TEXT NOT NULL DEFAULT '',
                players_b     TEXT NOT NULL DEFAULT '',
                target        INTEGER NOT NULL DEFAULT 21,
                win_by_two    INTEGER NOT NULL DEFAULT 0,
                state         TEXT NOT NULL DEFAULT 'running',
                score_a       INTEGER NOT NULL DEFAULT 0,
                score_b       INTEGER NOT NULL DEFAULT 0,
                winner        TEXT,
                referee       TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                finished_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS custom_match_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id      INTEGER NOT NULL,
                seq           INTEGER NOT NULL,
                scoring_team  TEXT NOT NULL,
                score_a       INTEGER NOT NULL,
                score_b       INTEGER NOT NULL,
                created_at    TEXT NOT NULL,
                UNIQUE (match_id, seq)
            );
            CREATE INDEX IF NOT EXISTS idx_custom_events_match ON custom_match_events(match_id, seq);
            -- 自定义对局同样不逐分 bump revision：比分变化走 /api/live 局部更新；
            -- 仅在创建 / 结束时递增 revision，让榜单与赛程页刷新出新的记录行。

            -- 数据版本号：名单与登录设置的任何写入都会自动递增，页面据此刷新。
            -- 不使用应用层手动维护，外部直接改库同样会触发。
            INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', '1');
            CREATE TRIGGER IF NOT EXISTS players_rev_insert AFTER INSERT ON players BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS players_rev_update AFTER UPDATE ON players BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS players_rev_delete AFTER DELETE ON players BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS teachers_rev_insert AFTER INSERT ON teachers BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS teachers_rev_update AFTER UPDATE ON teachers BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS teachers_rev_delete AFTER DELETE ON teachers BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS student_pins_rev_insert AFTER INSERT ON student_pins BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS student_pins_rev_update AFTER UPDATE ON student_pins BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS student_pins_rev_delete AFTER DELETE ON student_pins BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS matches_rev_insert AFTER INSERT ON matches BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS matches_rev_update AFTER UPDATE ON matches BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS matches_rev_delete AFTER DELETE ON matches BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS team_lineups_rev_insert AFTER INSERT ON team_lineups BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS team_lineups_rev_update AFTER UPDATE ON team_lineups BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS team_lineups_rev_delete AFTER DELETE ON team_lineups BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS match_lineups_rev_insert AFTER INSERT ON match_lineups BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS match_lineups_rev_update AFTER UPDATE ON match_lineups BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS match_lineups_rev_delete AFTER DELETE ON match_lineups BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS publications_rev_insert AFTER INSERT ON lineup_publications BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS publications_rev_update AFTER UPDATE ON lineup_publications BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS publications_rev_delete AFTER DELETE ON lineup_publications BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            -- 自定义对局：创建与结束各递增一次；逐分写入 custom_match_events 不入 revision。
            CREATE TRIGGER IF NOT EXISTS custom_matches_rev_insert AFTER INSERT ON custom_matches BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS custom_matches_rev_finish AFTER UPDATE OF state ON custom_matches BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            CREATE TRIGGER IF NOT EXISTS custom_matches_rev_delete AFTER DELETE ON custom_matches BEGIN
                UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision';
            END;
            """
        )
        cols = {row["name"] for row in db.execute("PRAGMA table_info(guesses)")}
        if "stake" not in cols:
            db.execute("ALTER TABLE guesses ADD COLUMN stake INTEGER NOT NULL DEFAULT 1")
        seeded = db.execute(
            "SELECT 1 FROM meta WHERE key = 'roster_seeded'"
        ).fetchone()
        _migrate_legacy_passcode(db)
    if not seeded:
        sync_roster()
        with connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('roster_seeded', '1')"
            )
    schedule_seeded = False
    with connection() as db:
        schedule_seeded = (
            db.execute("SELECT 1 FROM meta WHERE key = 'schedule_seeded'").fetchone()
            is not None
        )
    if not schedule_seeded:
        seed_schedule()
        with connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schedule_seeded', '1')"
            )
    _migrate_lineups_v2()
    _ensure_score_columns()
    _ensure_match_lineup_snapshots()
    _ensure_admin_shared_columns()


def _ensure_admin_shared_columns() -> None:
    """后台管理共用的字段：比赛名称/时间/状态、队员与老师的启用状态。"""
    with connection() as db:
        specs = (
            ("matches", "name", "TEXT NOT NULL DEFAULT ''"),
            ("matches", "match_time", "TEXT NOT NULL DEFAULT ''"),
            ("matches", "status", "TEXT NOT NULL DEFAULT '未开始'"),
            ("players", "enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("teachers", "enabled", "INTEGER NOT NULL DEFAULT 1"),
        )
        for table, column, ddl in specs:
            cols = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _ensure_match_lineup_snapshots() -> None:
    """升级兼容：历史已开赛局若没有阵容快照，按当前默认安排补一份。
    这样老库在启用场次级改人后，已结束局的历史人员也不会被后续修改带偏。"""
    with connection() as db:
        games = db.execute("SELECT match_id, game_no FROM score_games").fetchall()
        for g in games:
            sides = db.execute(
                "SELECT team_a, team_b FROM matches WHERE id = ?", (g["match_id"],)
            ).fetchone()
            if not sides:
                continue
            for team in (sides["team_a"], sides["team_b"]):
                exists = db.execute(
                    "SELECT 1 FROM match_lineups WHERE match_id = ? AND team = ? AND game_no = ?",
                    (g["match_id"], team, g["game_no"]),
                ).fetchone()
                if exists:
                    continue
                row = db.execute(
                    "SELECT player1, player2 FROM team_lineups WHERE team = ? AND game_no = ?",
                    (team, g["game_no"]),
                ).fetchone()
                if not row:
                    continue
                db.execute(
                    "INSERT INTO match_lineups (match_id, team, game_no, event, player1, player2) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (g["match_id"], team, g["game_no"],
                     GAMES[g["game_no"] - 1]["event"], row["player1"], row["player2"]),
                )


def _ensure_score_columns() -> None:
    """兼容旧库：补齐追分记分所需的列与展示版本号 rev。"""
    with connection() as db:
        cols = {row["name"] for row in db.execute("PRAGMA table_info(score_games)")}
        for name, ddl in (
            ("rev", "INTEGER NOT NULL DEFAULT 0"),
            ("start_a", "INTEGER NOT NULL DEFAULT 0"),
            ("start_b", "INTEGER NOT NULL DEFAULT 0"),
            ("target", "INTEGER NOT NULL DEFAULT 15"),
            ("switch_at", "INTEGER NOT NULL DEFAULT 8"),
        ):
            if name not in cols:
                db.execute(
                    "ALTER TABLE score_games ADD COLUMN %s %s" % (name, ddl)
                )


def _migrate_lineups_v2() -> None:
    """把旧版按场次的 72 行安排收敛成每组唯一一份（只执行一次）。

    每组在自己参与的三场里安排完全相同，取最小场次即可无损迁移。
    发布状态一律从「未发布」开始，由四位队长在首页各自确认发布。
    """
    with connection() as db:
        if db.execute(
            "SELECT 1 FROM meta WHERE key = 'lineup_v2_migrated'"
        ).fetchone():
            return
        for team in ROSTER:
            rows = db.execute(
                "SELECT game_no, event, player1, player2 FROM lineups "
                "WHERE team = ? AND match_id = ("
                "    SELECT MIN(match_id) FROM lineups WHERE team = ?"
                ") ORDER BY game_no",
                (team, team),
            ).fetchall()
            if len(rows) != len(GAMES):
                rows = default_lineup(team) or []
            for row in rows:
                db.execute(
                    "INSERT OR REPLACE INTO team_lineups "
                    "(team, game_no, event, player1, player2) VALUES (?, ?, ?, ?, ?)",
                    (
                        team,
                        row["game_no"],
                        row["event"],
                        row["player1"],
                        row["player2"],
                    ),
                )
        db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('lineup_v2_migrated', '1')"
        )


def sync_roster() -> None:
    """把代码里的初始名单全量写入数据库（仅用于首次 seed 或手动重置）。"""
    rows = []
    for team, names in ROSTER.items():
        for index, name in enumerate(names):
            rows.append(
                (
                    name,
                    team,
                    "女" if name in FEMALES else "男",
                    1 if CAPTAINS.get(name) == team else 0,
                    index,
                )
            )
    with connection() as db:
        db.executemany(
            """
            INSERT INTO players (name, team, gender, is_captain, sort_order)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                team       = excluded.team,
                gender     = excluded.gender,
                is_captain = excluded.is_captain,
                sort_order = excluded.sort_order
            """,
            rows,
        )
        marks = ",".join("?" for _ in rows)
        db.execute(
            f"DELETE FROM players WHERE name NOT IN ({marks})",
            [row[0] for row in rows],
        )
        db.executemany(
            """
            INSERT INTO teachers (name, sort_order) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET sort_order = excluded.sort_order
            """,
            [(name, index) for index, name in enumerate(TEACHERS)],
        )
        marks = ",".join("?" for _ in TEACHERS)
        db.execute(f"DELETE FROM teachers WHERE name NOT IN ({marks})", TEACHERS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_change(conn, operator, team, action, old_name, new_name=None, gender=None):
    conn.execute(
        "INSERT INTO roster_changes "
        "(occurred_at, operator, team, action, old_name, new_name, gender) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_now(), operator, team, action, old_name, new_name, gender),
    )


def current_revision() -> int:
    """当前数据版本号，由 SQLite 触发器自动维护，重启后仍保留。"""
    with connection() as db:
        row = db.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
    return int(row["value"]) if row else 0


def pin_version(name: str) -> int:
    """该账号的验证码版本；没改过码的账号固定为 1。"""
    with connection() as db:
        row = db.execute(
            "SELECT pin_version FROM student_pins WHERE name = ?", (name,)
        ).fetchone()
    return int(row["pin_version"]) if row else 1


def pin_is_custom(name: str) -> bool:
    """该账号是否已改过验证码（页面只提示状态，不显示明文）。"""
    with connection() as db:
        return (
            db.execute(
                "SELECT 1 FROM student_pins WHERE name = ?", (name,)
            ).fetchone()
            is not None
        )


def verify_student_pin(name: str, code: str) -> bool:
    """按姓名校验该账号自己的 4 位验证码；未改过码的账号用初始值。"""
    if not isinstance(code, str) or not code.isascii():
        return False
    with connection() as db:
        row = db.execute(
            "SELECT pass_hash FROM student_pins WHERE name = ?", (name,)
        ).fetchone()
    if row:
        return _check_passcode(code, row["pass_hash"])
    return hmac.compare_digest(code, DEFAULT_PIN)


def set_student_pin(name: str, new_code: str, operator: str = "") -> int:
    """修改指定账号的验证码，只递增该账号的版本号；返回新版本号。"""
    with connection() as db:
        row = db.execute(
            "SELECT pin_version FROM student_pins WHERE name = ?", (name,)
        ).fetchone()
        version = (int(row["pin_version"]) if row else 1) + 1
        db.execute(
            "INSERT INTO student_pins (name, pass_hash, pin_version, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "pass_hash = excluded.pass_hash, "
            "pin_version = excluded.pin_version, "
            "updated_at = excluded.updated_at",
            (name, _hash_passcode(new_code), version, _now()),
        )
        db.execute(
            "INSERT INTO login_logs (occurred_at, role, identity, team, outcome) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now(), "security", operator or name, None, "pin_changed"),
        )
    return version


def reset_student_pin(name: str) -> None:
    """管理员在命令行把某个账号恢复为初始 1234（其旧会话同时失效）。"""
    with connection() as db:
        db.execute("DELETE FROM student_pins WHERE name = ?", (name,))
        db.execute(
            "INSERT INTO login_logs (occurred_at, role, identity, team, outcome) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now(), "security", name, None, "pin_reset"),
        )


def recent_changes(team: str, limit: int = 6):
    """某组最近的名单变更（直接读数据库，供页面展示持久化结果）。"""
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM roster_changes WHERE team = ? ORDER BY id DESC LIMIT ?",
            (team, limit),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["display_time"] = (
                datetime.fromisoformat(item["occurred_at"])
                .astimezone()
                .strftime("%m-%d %H:%M")
            )
        except ValueError:
            item["display_time"] = item["occurred_at"][:16]
        result.append(item)
    return result


def replace_player(team: str, operator: str, old_name: str, new_name: str, gender: str):
    """用新队员替换本组某位队员，所有相关的人员信息同步替换为新的人。

    队长可自由选择新队员的性别（回退「只在同性间替换」的限制）。
    替换不受发布状态限制（已发布甚至赛程公布后也可换人）：
      * players      本组名单换名并更新性别（组别 / 排序 / 队长标记保留）
      * team_lineups 本组 6 局安排中该队员出现的每一处都换成新姓名，
        赛程页与各端页面由 revision 触发器驱动自动刷新
      * student_pins 删除旧姓名的自定义验证码，新姓名使用初始 1234
    若替换前本组安排已发布（赛程已公布），替换成功后自动撤回该组发布，
    使赛程暂时隐藏，待队长调整并重新发布后再公布。
    返回 (是否成功, 提示)。
    """
    with connection() as db:
        row = db.execute("SELECT * FROM players WHERE name = ?", (old_name,)).fetchone()
        if not row or row["team"] != team:
            return False, "该队员不在本组名单中"
        if row["is_captain"]:
            return False, "队长不能替换自己，如需变更请联系老师"
        taken = db.execute(
            "SELECT 1 FROM players WHERE name = ? AND name <> ?", (new_name, old_name)
        ).fetchone()
        if taken:
            return False, "该姓名已在名单中，不能重复使用"
        if gender not in ("男", "女"):
            gender = row["gender"]

        was_published = db.execute(
            "SELECT 1 FROM lineup_publications WHERE team = ?", (team,)
        ).fetchone() is not None

        db.execute(
            "UPDATE players SET name = ?, gender = ? WHERE name = ? AND team = ?",
            (new_name, gender, old_name, team),
        )
        db.execute(
            "UPDATE team_lineups SET player1 = ? WHERE team = ? AND player1 = ?",
            (new_name, team, old_name),
        )
        db.execute(
            "UPDATE team_lineups SET player2 = ? WHERE team = ? AND player2 = ?",
            (new_name, team, old_name),
        )
        # 场次级覆盖：未开赛的局同步换成新队员；已开赛局保留开赛时固化的历史姓名。
        overrides = db.execute(
            "SELECT match_id, game_no, player1, player2 FROM match_lineups WHERE team = ?",
            (team,),
        ).fetchall()
        for ov in overrides:
            started = db.execute(
                "SELECT 1 FROM score_games WHERE ref = ?",
                (match_ref(ov["match_id"], ov["game_no"]),),
            ).fetchone()
            if started:
                continue
            db.execute(
                "UPDATE match_lineups SET player1 = ?, player2 = ? "
                "WHERE match_id = ? AND team = ? AND game_no = ?",
                (
                    new_name if ov["player1"] == old_name else ov["player1"],
                    new_name if ov["player2"] == old_name else ov["player2"],
                    ov["match_id"], team, ov["game_no"],
                ),
            )
        db.execute("DELETE FROM student_pins WHERE name = ?", (old_name,))
        _log_change(db, operator, team, "replace", old_name, new_name, gender)
        if was_published:
            db.execute("DELETE FROM lineup_publications WHERE team = ?", (team,))
            return True, (
                f"已将 {old_name} 替换为 {new_name}（{gender}）；本组 6 局安排、赛程与验证码账号已同步。"
                f"由于本组安排此前已发布，系统已自动撤回发布，请调整后重新发布。"
            )
    return True, (
        f"已将 {old_name} 替换为 {new_name}（{gender}）；本组 6 局安排、赛程与验证码账号已同步，"
        f"{new_name} 用初始验证码 1234 登录"
    )


def delete_player(team: str, operator: str, name: str):
    """从本组名单中删除一名队员。返回 (是否成功, 提示)。"""
    with connection() as db:
        row = db.execute("SELECT * FROM players WHERE name = ?", (name,)).fetchone()
        if not row or row["team"] != team:
            return False, "该队员不在本组名单中"
        if row["is_captain"]:
            return False, "队长不能删除自己，如需变更请联系老师"
        db.execute("DELETE FROM players WHERE name = ? AND team = ?", (name, team))
        _log_change(db, operator, team, "delete", name)
    return True, f"已从 {team} 组删除 {name}"


def find_player(name: str):
    with connection() as db:
        row = db.execute("SELECT * FROM players WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def is_teacher(name: str) -> bool:
    with connection() as db:
        return db.execute(
            "SELECT 1 FROM teachers WHERE name = ? AND enabled = 1", (name,)
        ).fetchone() is not None


def is_player_enabled(name: str) -> bool:
    """后台禁用的队员不能登录；名单外的观众账号不受限制。"""
    with connection() as db:
        row = db.execute("SELECT enabled FROM players WHERE name = ?", (name,)).fetchone()
    return bool(row["enabled"]) if row else True


def team_members(team: str):
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM players WHERE team = ? ORDER BY sort_order", (team,)
        ).fetchall()
    return [dict(row) for row in rows]


def all_teams():
    with connection() as db:
        rows = db.execute("SELECT * FROM players ORDER BY team, sort_order").fetchall()
    grouped = {team: [] for team in ROSTER}
    for row in rows:
        grouped.setdefault(row["team"], []).append(dict(row))
    return grouped


def log_login(role: str, identity: str, team: str | None, outcome: str) -> None:
    with connection() as db:
        db.execute(
            "INSERT INTO login_logs (occurred_at, role, identity, team, outcome) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now(), role, identity, team, outcome),
        )


# ---------------- 赛程编排 ----------------

def default_lineup(team: str):
    """按当前名单自动生成一组合法的 6 局安排。

    满足：每人 1~2 次、相邻两局不重复、两局同类项目至少换 1 人、
    混双 1 男 1 女、男双/男单限男生。名单不满足最低配置时返回 None。
    """
    members = team_members(team)
    females = [m["name"] for m in members if m["gender"] == "女"]
    males = [m["name"] for m in members if m["gender"] == "男"]
    if not females or len(males) < 4:
        return None
    first_female = females[0]
    second_female = females[1] if len(females) > 1 else females[0]
    if len(males) >= 5:
        game4, game6, game5_male = (males[3], males[4]), males[2], males[1]
    else:  # 只有 4 名男生时，每人必须打满 2 次
        game4, game6, game5_male = (males[3], males[1]), males[3], males[2]
    pairs = [
        (first_female, males[0]),
        (males[1], males[2]),
        (males[0], None),
        game4,
        (second_female, game5_male),
        (game6, None),
    ]
    return [
        {
            "game_no": game["no"],
            "event": game["event"],
            "player1": pair[0],
            "player2": pair[1],
        }
        for game, pair in zip(GAMES, pairs)
    ]


def seed_schedule() -> None:
    """首次启动时写入 6 场对阵与每场两组的默认安排；之后以数据库为准。"""
    defaults = {team: default_lineup(team) for team in ROSTER}
    with connection() as db:
        for match in MATCH_PLAN:
            db.execute(
                "INSERT OR IGNORE INTO matches (id, round_no, court, team_a, team_b) "
                "VALUES (?, ?, ?, ?, ?)",
                (match["id"], match["round"], match["court"], *match["teams"]),
            )
            for team in match["teams"]:
                rows = defaults.get(team)
                if not rows:
                    continue
                db.executemany(
                    "INSERT OR IGNORE INTO lineups "
                    "(match_id, team, game_no, event, player1, player2) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            match["id"],
                            team,
                            row["game_no"],
                            row["event"],
                            row["player1"],
                            row["player2"],
                        )
                        for row in rows
                    ],
                )


def all_matches():
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM matches ORDER BY round_no, court"
        ).fetchall()
    return [dict(row) for row in rows]


def team_lineup(team: str):
    """该组唯一的 6 局出场安排（不区分对手、不区分场次）。"""
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM team_lineups WHERE team = ? ORDER BY game_no", (team,)
        ).fetchall()
    return [dict(row) for row in rows]


def team_lineup_map(team: str):
    return {row["game_no"]: row for row in team_lineup(team)}


def match_ref(match_id: int, game_no: int) -> str:
    return "%d-%d" % (match_id, game_no)


def is_game_started(match_id: int, game_no: int) -> bool:
    """该场该局是否已开赛；一旦开赛，上场人员锁定，队长不能再改。"""
    with connection() as db:
        return db.execute(
            "SELECT 1 FROM score_games WHERE ref = ?",
            (match_ref(match_id, game_no),),
        ).fetchone() is not None


def match_lineup_overrides(match_id: int, team: str) -> dict[int, dict]:
    """只取该场该组的场次级人员覆盖行。"""
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM match_lineups WHERE match_id = ? AND team = ? ORDER BY game_no",
            (match_id, team),
        ).fetchall()
    return {row["game_no"]: dict(row) for row in rows}


def effective_lineup_map(match_id: int, team: str) -> dict[int, dict]:
    """该场该组的实际安排：场次级覆盖优先，未覆盖的局回退到本组唯一安排。"""
    result = {no: dict(row) for no, row in team_lineup_map(team).items()}
    result.update(match_lineup_overrides(match_id, team))
    return result


def all_effective_lineups() -> dict[tuple[int, str], dict[int, dict]]:
    """一次性构建全部 (场次, 组) 的实际安排，赛程页/裁判台/榜单共用同一口径。"""
    result: dict[tuple[int, str], dict[int, dict]] = {}
    with connection() as db:
        defaults = db.execute("SELECT * FROM team_lineups").fetchall()
        overrides = db.execute("SELECT * FROM match_lineups").fetchall()
        matches = db.execute("SELECT * FROM matches").fetchall()
    by_team: dict[str, dict[int, dict]] = {}
    for row in defaults:
        by_team.setdefault(row["team"], {})[row["game_no"]] = dict(row)
    by_match_team: dict[tuple[int, str], dict[int, dict]] = {}
    for row in overrides:
        by_match_team.setdefault((row["match_id"], row["team"]), {})[row["game_no"]] = dict(row)
    for m in matches:
        for team in (m["team_a"], m["team_b"]):
            merged = {no: dict(v) for no, v in by_team.get(team, {}).items()}
            merged.update(by_match_team.get((m["id"], team), {}))
            result[(m["id"], team)] = merged
    return result


def validate_match_game(team: str, game_no: int, player1: str, player2: str,
                        extra_gender: dict | None = None) -> list[str]:
    """场次级单局改人的校验：只做基本数据校验，阵容限制仅保留混双 1 男 1 女。

    开赛后的临时改人不受默认安排那套规则约束（相邻两局不连续上场、
    两局同类项目至少换 1 人、每人 1~2 次、男双/男单限男生）。
    这里只保证：局次有效、姓名在本组名单内、人数够且不重复，
    外加混双必须一男一女。extra_gender 用于「同一次提交里新增的队员」，
    它们尚未落库，先按表单填写的性别参与校验。
    """
    game = next((g for g in GAMES if g["no"] == game_no), None)
    if not game:
        return ["无效的局次"]
    gender = {m["name"]: m["gender"] for m in team_members(team)}
    if extra_gender:
        gender.update(extra_gender)
    errors: list[str] = []
    p1, p2 = (player1 or "").strip(), (player2 or "").strip()
    for name in (p1, p2):
        if name and name not in gender:
            errors.append(f"{name} 不在 {team} 组当前名单中")
    if game["size"] == 1:
        if not p1:
            errors.append(f"{game['event']}未选择队员")
    else:
        if not p1 or not p2:
            errors.append(f"{game['event']}需要两名队员")
        if p1 and p2 and p1 == p2:
            errors.append("同一名队员不能出现两次")
        if game["event"] == "混双":
            g1, g2 = gender.get(p1), gender.get(p2)
            if g1 and g2 and {g1, g2} != {"男", "女"}:
                errors.append("混双需 1 男 1 女")
    return errors


def save_match_lineup(match_id: int, team: str, operator: str,
                      game_no: int, player1: str, player2: str,
                      new_name: str = "", new_gender: str = "", new_slot: str = "1"):
    """开赛后队长改「未开赛小局」的本组人员；只写覆盖行，不动默认安排和其他场次。

    new_name / new_gender 非空时，先把这位新队员正式加入本组名单（初始验证码
    1234），再用他/她替换本局的指定位置。建号、名单变更记录与覆盖行在同一个
    事务里完成，避免只加了人却没写上进场阵容。新增队员不会撤回本组发布。
    """
    match = next((m for m in all_matches() if m["id"] == match_id), None)
    if not match or team not in (match["team_a"], match["team_b"]):
        return False, "该场次不属于本组"
    if not publication_status().get(team, {}).get("published"):
        return False, "本组安排尚未发布，请先在首页确认发布"
    if is_game_started(match_id, game_no):
        return False, "这一局已经开赛，上场人员已锁定"
    game = next((g for g in GAMES if g["no"] == game_no), None)
    if not game:
        return False, "无效的局次"

    p1 = (player1 or "").strip()
    p2 = (player2 or "").strip() or None if game["size"] == 2 else None
    new_name = (new_name or "").strip()
    extra_gender = None
    if new_name:
        if len(new_name) > 12:
            return False, "新队员姓名请控制在 12 个字以内"
        if new_gender not in ("男", "女"):
            return False, "请选择新队员的性别"
        extra_gender = {new_name: new_gender}
        if game["size"] == 1 or str(new_slot).strip() == "1":
            p1 = new_name
        else:
            p2 = new_name

    errors = validate_match_game(team, game_no, p1, p2 or "", extra_gender)
    if errors:
        return False, errors[0]

    with connection() as db:
        if new_name:
            if db.execute("SELECT 1 FROM players WHERE name = ?", (new_name,)).fetchone():
                return False, "该姓名已在名单中，请直接从下拉选择"
            order = db.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM players WHERE team = ?",
                (team,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO players (name, team, gender, is_captain, sort_order) "
                "VALUES (?, ?, ?, 0, ?)",
                (new_name, team, new_gender, order),
            )
            _log_change(db, operator, team, "add", new_name, new_name, new_gender)
        db.execute(
            "INSERT INTO match_lineups (match_id, team, game_no, event, player1, player2) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(match_id, team, game_no) DO UPDATE SET "
            "event = excluded.event, player1 = excluded.player1, player2 = excluded.player2",
            (match_id, team, game_no, game["event"], p1, p2),
        )
    message = f"已更新本场第 {game_no} 局上场人员（仅影响这一场这一局）"
    if new_name:
        message += f"；{new_name}（{new_gender}）已加入 {team} 组名单，初始验证码 1234"
    return True, message


def reset_match_lineup(match_id: int, team: str, operator: str, game_no: int):
    """该场该局恢复默认安排；已开赛局不允许恢复。"""
    if is_game_started(match_id, game_no):
        return False, "这一局已经开赛，不能恢复默认"
    with connection() as db:
        removed = db.execute(
            "DELETE FROM match_lineups WHERE match_id = ? AND team = ? AND game_no = ?",
            (match_id, team, game_no),
        ).rowcount
    return True, "已恢复本场这一局的默认安排" if removed else "这一局已经是默认安排"


def publication_status():
    """四组的发布状态；赛程页与首页都按这个判断是否公布。"""
    with connection() as db:
        rows = db.execute("SELECT * FROM lineup_publications").fetchall()
    result = {
        team: {"published": False, "display_time": "", "operator": ""}
        for team in ROSTER
    }
    for row in rows:
        try:
            shown = (
                datetime.fromisoformat(row["published_at"])
                .astimezone()
                .strftime("%m-%d %H:%M")
            )
        except ValueError:
            shown = row["published_at"][:16]
        result[row["team"]] = {
            "published": True,
            "display_time": shown,
            "operator": row["operator"],
        }
    return result


def all_published() -> bool:
    return all(item["published"] for item in publication_status().values())


def lineup_counts(team: str, rows):
    """本场每位队员的出场次数，供页面展示。"""
    counts: dict[str, int] = {}
    for row in rows:
        for name in (row.get("player1"), row.get("player2")):
            if name:
                counts[name] = counts.get(name, 0) + 1
    return [
        {"name": m["name"], "gender": m["gender"], "count": counts.get(m["name"], 0)}
        for m in team_members(team)
    ]


def validate_lineup(team: str, rows):
    """返回 (硬错误列表, 软警告列表)。硬错误阻止保存，软警告只提示。

    硬约束：姓名在本组名单内、项目人数、混双 1 男 1 女、男双/男单限男生、
    同局不重复、相邻两局不连续上场、两局同类项目至少更换 1 人。
    软约束：每人出场 1~2 次（人数偏少的组调整时容易突破，允许保存但提示）。
    """
    members = team_members(team)
    gender = {m["name"]: m["gender"] for m in members}
    errors: list[str] = []
    warnings: list[str] = []
    if len(rows) != len(GAMES):
        return ["安排必须为 6 局"], warnings

    sets: list[set[str]] = []
    for index, row in enumerate(rows):
        game = GAMES[index]
        no = game["no"]
        p1 = (row.get("player1") or "").strip()
        p2 = (row.get("player2") or "").strip() if game["size"] == 2 else ""
        for name in (p1, p2):
            if name and name not in gender:
                errors.append(f"第 {no} 局的 {name} 不在 {team} 组当前名单中")
        if game["size"] == 1:
            if not p1:
                errors.append(f"第 {no} 局男单未选择队员")
            elif gender.get(p1) != "男":
                errors.append(f"第 {no} 局男单必须由男生出战")
            people = [p1] if p1 else []
        else:
            if not p1 or not p2:
                errors.append(f"第 {no} 局{game['event']}需要两名队员")
            if p1 and p2 and p1 == p2:
                errors.append(f"第 {no} 局同一名队员不能出现两次")
            g1, g2 = gender.get(p1), gender.get(p2)
            if game["event"] == "混双":
                if g1 and g2 and {g1, g2} != {"男", "女"}:
                    errors.append(f"第 {no} 局混双需 1 男 1 女")
            elif (g1 and g1 != "男") or (g2 and g2 != "男"):
                errors.append(f"第 {no} 局男双需由男生出战")
            people = [p for p in (p1, p2) if p]
        row["player1"], row["player2"] = p1, (p2 or None)
        sets.append(set(people))

    for i in range(len(sets) - 1):
        overlap = sets[i] & sets[i + 1]
        if overlap:
            errors.append(
                f"第 {i + 1}、{i + 2} 局有队员连续上场：{'、'.join(sorted(overlap))}"
            )
    for event in ("混双", "男双", "男单"):
        indexes = [i for i, game in enumerate(GAMES) if game["event"] == event]
        if sets[indexes[0]] and sets[indexes[0]] == sets[indexes[1]]:
            errors.append(f"两局{event}阵容完全相同，至少更换 1 名队员")

    counts: dict[str, int] = {}
    for group in sets:
        for name in group:
            counts[name] = counts.get(name, 0) + 1
    for name in gender:
        times = counts.get(name, 0)
        if times == 0:
            warnings.append(f"{name} 本场未上场")
        elif times > 2:
            warnings.append(f"{name} 本场出场 {times} 次（上限 2 次）")
    return errors, warnings


def _write_team_lineup(team: str, operator: str, rows, action: str):
    """写入该组唯一安排；已发布时拒绝改动，需先撤回发布。"""
    errors, warnings = validate_lineup(team, rows)
    if errors:
        return False, errors[0], warnings
    if publication_status().get(team, {}).get("published"):
        return False, "本组安排已发布，请先撤回发布再修改", warnings
    with connection() as db:
        old = {
            row["game_no"]: (row["player1"], row["player2"])
            for row in db.execute(
                "SELECT * FROM team_lineups WHERE team = ?", (team,)
            )
        }
        changed = []
        for row in rows:
            no = row["game_no"]
            new = (row["player1"], row["player2"])
            if old.get(no) == new:
                continue
            db.execute(
                "INSERT INTO team_lineups (team, game_no, event, player1, player2) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(team, game_no) DO UPDATE SET "
                "event = excluded.event, player1 = excluded.player1, "
                "player2 = excluded.player2",
                (team, no, GAMES[no - 1]["event"], *new),
            )
            before = "、".join(x for x in old.get(no, ("", "")) if x) or "空"
            after = "、".join(x for x in new if x) or "空"
            changed.append(f"第{no}局 {before} → {after}")
    label = "已恢复本组默认编排" if action == "reset" else "本组安排已保存"
    if not changed:
        label += "（无改动）"
    return True, label, warnings


def save_team_lineup(team: str, operator: str, rows):
    return _write_team_lineup(team, operator, rows, "update")


def reset_team_lineup(team: str, operator: str):
    rows = default_lineup(team)
    if not rows:
        return False, "本组女生不足或男生少于 4 人，无法生成默认编排", []
    return _write_team_lineup(team, operator, rows, "reset")


def publish_lineup(team: str, operator: str, publish: bool):
    """发布 / 撤回本组安排；match_id=0 表示这是唯一安排而非某一场。"""
    with connection() as db:
        if publish:
            db.execute(
                "INSERT INTO lineup_publications (team, published_at, operator) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(team) DO UPDATE SET "
                "published_at = excluded.published_at, operator = excluded.operator",
                (team, _now(), operator),
            )
            return True, f"{team} 组安排已发布"
        removed = db.execute(
            "DELETE FROM lineup_publications WHERE team = ?", (team,)
        ).rowcount
    return True, f"{team} 组已撤回发布，可继续调整"


# ----------------------------- 追分记分引擎 -----------------------------
"""追分版：一个场次的 6 局共用一份累计比分，跨局继承、绝不归零。

每局的结束分数是「绝对累计分门槛」，对双方一视同仁：
    第 1~6 局目标分 = 15 / 30 / 45 / 60 / 75 / 90
    换边提示分      =  8 / 23 / 38 / 53 / 68 / 83
裁判每点一次加分写一条逐分流水；任一方累计分达到本局目标，本局即结束，
比分原样带入下一局。第 6 局打到 90 分整场完赛。

比赛计分不存储「本局胜方」，winner 列仅为兼容旧库保留且永远为 NULL。
竞猜独立按本局新增十五分的先后顺序推导结果，不改变追分制记分规则。
整场胜场改为实时推导：逐分流水中第一次累计到 90 分的一方记 1 胜，
只统计 6 局全部结束的场次，不落库。
"""
GAME_TARGETS = {g["no"]: 15 * g["no"] for g in GAMES}
GAME_SWITCH = {g["no"]: 15 * (g["no"] - 1) + 8 for g in GAMES}
_score_lock = threading.RLock()


def save_guess(name: str, kind: str, ref: str, pick: str, stake: int = 1) -> bool:
    """写入（或覆盖）某用户的单条竞猜，并快照该用户的下注分数。

    stake（所有人基础分为 1分）同时是竞猜计分公式里的「基础分数」。
    同一 (name, kind, ref) 只保留最新一次。
    """
    now = _now()
    with _score_lock, connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM tournament_result WHERE id = 1").fetchone():
            return False
        if kind == "champion":
            locked = db.execute("SELECT 1 FROM score_games LIMIT 1").fetchone()
        else:
            locked = db.execute("SELECT 1 FROM score_games WHERE ref = ?", (ref,)).fetchone()
        if locked:
            return False
        db.execute(
            """
            INSERT INTO guesses (name, kind, ref, pick, stake, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, kind, ref) DO UPDATE SET
                pick = excluded.pick,
                stake = excluded.stake,
                updated_at = excluded.updated_at
            """,
            (name, kind, ref, pick, int(stake), now, now),
        )
    return True


def delete_guess(name: str, kind: str, ref: str) -> bool:
    """删除某用户的单条竞猜（取消选择），无记录时静默忽略。"""
    with _score_lock, connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM tournament_result WHERE id = 1").fetchone():
            return False
        if kind == "champion":
            locked = db.execute("SELECT 1 FROM score_games LIMIT 1").fetchone()
        else:
            locked = db.execute("SELECT 1 FROM score_games WHERE ref = ?", (ref,)).fetchone()
        if locked:
            return False
        db.execute(
            "DELETE FROM guesses WHERE name = ? AND kind = ? AND ref = ?",
            (name, kind, ref),
        )
    return True


def get_user_guesses(name: str):
    """返回该用户已提交的竞猜，按类型分组供页面渲染。

    返回 {"champion": 组别或 None, "games": {ref: pick}}。
    """
    with connection() as db:
        rows = db.execute(
            "SELECT kind, ref, pick FROM guesses WHERE name = ?", (name,)
        ).fetchall()
    champion = None
    games: dict[str, str] = {}
    for row in rows:
        if row["kind"] == "champion":
            champion = row["pick"]
        elif row["kind"] == "game":
            games[row["ref"]] = row["pick"]
    return {"champion": champion, "games": games}


def champion_counts() -> dict[str, int]:
    """各组被猜夺冠的人次统计（只显示计数，不暴露具体姓名）。"""
    with connection() as db:
        rows = db.execute(
            "SELECT pick, COUNT(*) AS cnt FROM guesses "
            "WHERE kind = 'champion' GROUP BY pick"
        ).fetchall()
    return {row["pick"]: row["cnt"] for row in rows}


def has_started_game() -> bool:
    with connection() as conn:
        return conn.execute("SELECT 1 FROM score_games UNION ALL SELECT 1 FROM tournament_result LIMIT 1").fetchone() is not None


def guess_winner(game: dict, match: dict, events: list) -> str | None:
    """本局增量率先达到十五分的一方；承接分不参与，撤销后按现存流水重算。"""
    for event in events:
        if event["score_a"] - (game["start_a"] or 0) >= 15:
            return match["team_a"]
        if event["score_b"] - (game["start_b"] or 0) >= 15:
            return match["team_b"]
    # 无流水的旧数据仅在一方达标时可确定；双方达标无法推断先后。
    if not events:
        a = game["score_a"] - (game["start_a"] or 0) >= 15
        b = game["score_b"] - (game["start_b"] or 0) >= 15
        if a != b:
            return match["team_a"] if a else match["team_b"]
    return None


def guess_results(viewer_name: str = "") -> dict:
    games = all_score_games()
    matches = {m["id"]: m for m in all_matches()}
    with connection() as conn:
        rows = conn.execute("SELECT * FROM score_events ORDER BY ref, seq").fetchall()
        guesses = conn.execute("SELECT * FROM guesses WHERE kind = 'game'").fetchall()
    events = {}
    stakes = {}
    mine = {}
    for row in rows:
        events.setdefault(row["ref"], []).append(row)
    for row in guesses:
        stat = stakes.setdefault(row["ref"], {}).setdefault(row["pick"], {"count": 0, "stake": 0})
        stat["count"] += 1
        stat["stake"] += row["stake"]
        if row["name"] == viewer_name:
            mine[row["ref"]] = row
    result = {}
    for ref, game in games.items():
        match = matches.get(game["match_id"])
        if not match:
            continue
        winner = guess_winner(game, match, events.get(ref, []))
        stats = stakes.get(ref, {})
        a_stat = stats.get(match["team_a"], {"count": 0, "stake": 0})
        b_stat = stats.get(match["team_b"], {"count": 0, "stake": 0})
        predict = None
        if a_stat["stake"] + b_stat["stake"] > 0:
            predict = {"a_count": a_stat["count"], "b_count": b_stat["count"],
                       "a_stake": a_stat["stake"], "b_stake": b_stat["stake"]}
        pick = mine.get(ref)
        expected = None
        settled = None
        if pick:
            own = stakes[ref][pick["pick"]]
            other_team = match["team_b"] if pick["pick"] == match["team_a"] else match["team_a"]
            other = stakes[ref].get(other_team, {"stake": 0})
            expected = round(guess_gain(pick["stake"], own["count"], other["stake"]), 2)
            if game["state"] == "finished" and winner:
                settled = {"win": pick["pick"] == winner,
                           "gain": expected if pick["pick"] == winner else 0.0}
        result[ref] = {"state": game["state"], "winner": winner,
                       "my_pick": pick["pick"] if pick else None,
                       "expected": expected, "settled": settled, "predict": predict,
                       "gain_a": game["score_a"] - (game["start_a"] or 0),
                       "gain_b": game["score_b"] - (game["start_b"] or 0)}
    return result


def guess_gain(stake: int, own_count: int, other_stake: int) -> float:
    """单局猜中所得分数的唯一口径。

    自己的下注分数 + 对方组押注分数之和 ÷ 己方押注人数。
    赛程卡片的预计得分、结算结果与积分榜单共用，避免三处公式漂移。
    此处不四舍五入：榜单按局累加后统一格式化，卡片展示时再保留两位小数。
    """
    return float(stake) + (float(other_stake) / own_count if own_count else 0.0)


def guess_stake_map(ref: str) -> dict[str, dict]:
    """某局各组的押注统计：人数与下注分数之和。"""
    with connection() as db:
        rows = db.execute(
            "SELECT pick, COUNT(*) AS cnt, COALESCE(SUM(stake), 0) AS total "
            "FROM guesses WHERE kind = 'game' AND ref = ? GROUP BY pick", (ref,)
        ).fetchall()
    return {row["pick"]: {"count": row["cnt"], "stake": row["total"]}
            for row in rows}


def game_rule(game_no: int) -> dict:
    """某局的结束目标分与换边提示分。"""
    return {"target": GAME_TARGETS[game_no], "switch_at": GAME_SWITCH[game_no]}


def _bump_rev(db, ref: str) -> None:
    db.execute("UPDATE score_games SET rev = rev + 1 WHERE ref = ?", (ref,))


def score_game(ref: str):
    """单局记分主记录；未开赛返回 None。"""
    with connection() as db:
        row = db.execute("SELECT * FROM score_games WHERE ref = ?", (ref,)).fetchone()
    return dict(row) if row else None


def score_game_state(ref: str) -> str:
    """idle（未开赛）/ running（进行中）/ finished（已结束）。"""
    row = score_game(ref)
    return row["state"] if row else "idle"


def all_score_games() -> dict[str, dict]:
    with connection() as db:
        rows = db.execute("SELECT * FROM score_games").fetchall()
    return {row["ref"]: dict(row) for row in rows}


def has_running_game() -> bool:
    """是否存在进行中的比赛：导航提示点与轻量状态接口共用同一口径。"""
    with connection() as db:
        row = db.execute(
            "SELECT 1 FROM score_games WHERE state = 'running' LIMIT 1"
        ).fetchone()
    return row is not None


def running_ref_for(referee: str) -> str | None:
    """某裁判当前正在执裁、尚未结束的局；多局时取最近开赛的一局。

    重新进入裁判台时用它自动恢复记分界面；只有本人开赛且仍在进行中的局才算，
    别人的局、已结束的局都不会被自动打开。
    """
    with connection() as db:
        row = db.execute(
            "SELECT ref FROM score_games "
            "WHERE state = 'running' AND referee = ? "
            "ORDER BY started_at DESC, ref DESC LIMIT 1",
            (referee,),
        ).fetchone()
    return row["ref"] if row else None


def score_events_for(ref: str) -> list[dict]:
    with connection() as db:
        rows = db.execute(
            "SELECT seq, scoring_team, score_a, score_b FROM score_events "
            "WHERE ref = ? ORDER BY seq", (ref,)
        ).fetchall()
    return [dict(row) for row in rows]


def start_score_game(ref: str, match_id: int, game_no: int,
                     court_input: str, referee: str) -> tuple[bool, str]:
    """按局次顺序开赛：继承上一局结束时的累计比分作为本局起始比分。

    第 N 局（N>1）要求第 N-1 局已确认结束，否则无法继承比分；
    同一场次同时只允许一局处于进行中。
    """
    with _score_lock, connection() as db:
        exists = db.execute(
            "SELECT 1 FROM score_games WHERE ref = ?", (ref,)
        ).fetchone()
        if exists:
            return False, "这一局已经开赛或已结束，不能重复开赛"
        prev = None
        if game_no > 1:
            prev = db.execute(
                "SELECT * FROM score_games WHERE match_id = ? AND game_no = ?",
                (match_id, game_no - 1),
            ).fetchone()
            if not prev or prev["state"] != "finished":
                return False, "请先结束第 %d 局，再开始第 %d 局" % (game_no - 1, game_no)
        running = db.execute(
            "SELECT game_no FROM score_games "
            "WHERE match_id = ? AND state = 'running' LIMIT 1", (match_id,)
        ).fetchone()
        if running:
            return False, "本场次第 %d 局正在进行中，请先结束该局" % running["game_no"]
        start_a = prev["score_a"] if prev else 0
        start_b = prev["score_b"] if prev else 0
        # 开赛瞬间固化双方本局的实际阵容（含队长的场次级改人）。
        # 此后默认安排或名单变化都不会改写已开赛局的历史上场人员。
        sides = db.execute(
            "SELECT team_a, team_b FROM matches WHERE id = ?", (match_id,)
        ).fetchone()
        if sides:
            for team in (sides["team_a"], sides["team_b"]):
                default = db.execute(
                    "SELECT player1, player2 FROM team_lineups WHERE team = ? AND game_no = ?",
                    (team, game_no),
                ).fetchone()
                override = db.execute(
                    "SELECT player1, player2 FROM match_lineups "
                    "WHERE match_id = ? AND team = ? AND game_no = ?",
                    (match_id, team, game_no),
                ).fetchone()
                actual = override or default
                if not actual:
                    continue
                db.execute(
                    "INSERT INTO match_lineups (match_id, team, game_no, event, player1, player2) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(match_id, team, game_no) DO UPDATE SET "
                    "event = excluded.event, player1 = excluded.player1, player2 = excluded.player2",
                    (match_id, team, game_no, GAMES[game_no - 1]["event"],
                     actual["player1"], actual["player2"]),
                )
        rule = game_rule(game_no)
        db.execute(
            "INSERT INTO score_games (ref, match_id, game_no, court_input, referee, "
            "state, start_a, start_b, score_a, score_b, target, switch_at, "
            "switched, rev, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, 0, 1, ?)",
            (ref, match_id, game_no, court_input, referee,
             start_a, start_b, start_a, start_b,
             rule["target"], rule["switch_at"], _now()),
        )
    return True, "比赛已开始"


def _serve_hint(conn, ref: str, team_a: str, team_b: str) -> dict:
    """按羽毛球双打发球轮转口径给出「换边 / 哪侧发球」提示。

    - 发球方 = 最近一分的得分方；本局尚无流水时默认 team_a 发球。
    - 最近两分由同一方拿下（连续得分）= 发球方赢下自己的发球轮，需换发球区；
      否则是接发方得分转为发球，不换边，只提示由哪侧发球。
    - 发球区按发球方当前累计分的奇偶判断：偶数右区、奇数左区。
    """
    row = conn.execute(
        "SELECT score_a, score_b FROM score_games WHERE ref = ?", (ref,)
    ).fetchone()
    if not row:
        return {"team": "", "side": "", "streak": False, "text": ""}
    events = conn.execute(
        "SELECT scoring_team FROM score_events WHERE ref = ? ORDER BY seq DESC LIMIT 2",
        (ref,),
    ).fetchall()
    server = events[0]["scoring_team"] if events else team_a
    streak = bool(len(events) >= 2 and events[1]["scoring_team"] == server)
    own_score = row["score_a"] if server == team_a else row["score_b"]
    side = "右" if own_score % 2 == 0 else "左"
    text = (
        "%s组连续得分，%s组换边，由%s侧发球" % (server, server, side)
        if streak else "%s组不换边，由%s侧发球" % (server, side)
    )
    return {"team": server, "side": side, "streak": streak, "text": text}


def serve_hint(ref: str, team_a: str, team_b: str) -> dict:
    """裁判台进入记分卡片时使用的发球提示（独立读取连接）。"""
    with connection() as conn:
        return _serve_hint(conn, ref, team_a, team_b)


def add_point(ref: str, scoring_team: str, team_a: str, team_b: str) -> dict:
    """加一分：追加逐分流水，累计比分 +1，返回换边 / 达到本局目标的提示。

    team_a / team_b 是赛程里的绝对组别；换边只改界面位置，
    这里永远按绝对组别落库，不会因对调把分加错组。
    """
    with _score_lock, connection() as db:
        row = db.execute(
            "SELECT * FROM score_games WHERE ref = ?", (ref,)
        ).fetchone()
        if not row or row["state"] != "running":
            return {"ok": False, "error": "这一局不在进行中"}
        if scoring_team not in (team_a, team_b):
            return {"ok": False, "error": "无效的得分组别"}
        a, b = row["score_a"], row["score_b"]
        if scoring_team == team_a:
            a += 1
        else:
            b += 1
        seq = db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM score_events WHERE ref = ?", (ref,)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO score_events "
            "(ref, seq, scoring_team, score_a, score_b, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ref, seq, scoring_team, a, b, _now()),
        )
        rule = game_rule(row["game_no"])
        target = row["target"] or rule["target"]
        switch_at = row["switch_at"] or rule["switch_at"]
        # 绝对累计分门槛：任一方到本局目标即可结束本局（另一方可以低于目标）
        need_switch = bool(not row["switched"] and max(a, b) >= switch_at)
        reached_end = max(a, b) >= target
        db.execute(
            "UPDATE score_games SET score_a = ?, score_b = ? WHERE ref = ?",
            (a, b, ref),
        )
        _bump_rev(db, ref)
        return {
            "ok": True, "ref": ref, "score_a": a, "score_b": b,
            "seq": seq, "state": "running", "game_no": row["game_no"],
            "start_a": row["start_a"], "start_b": row["start_b"],
            "target": target, "switch_at": switch_at,
            "need_switch": need_switch, "reached_end": reached_end,
            "serve": _serve_hint(db, ref, team_a, team_b),
        }


def undo_point(ref: str) -> dict:
    """回退：删掉本局最后一条得分流水；最多退回到本局继承来的起始比分。

    已确认结束的局不可回退（下一局可能已经继承了它的比分）。
    """
    with _score_lock, connection() as db:
        row = db.execute(
            "SELECT * FROM score_games WHERE ref = ?", (ref,)
        ).fetchone()
        if not row or row["state"] != "running":
            return {"ok": False, "error": "这一局不在进行中或已确认结束"}
        last = db.execute(
            "SELECT seq FROM score_events WHERE ref = ? ORDER BY seq DESC LIMIT 1",
            (ref,),
        ).fetchone()
        if not last:
            return {"ok": False, "error": "已回到本局起始比分"}
        db.execute(
            "DELETE FROM score_events WHERE ref = ? AND seq = ?", (ref, last["seq"])
        )
        prev = db.execute(
            "SELECT score_a, score_b FROM score_events "
            "WHERE ref = ? ORDER BY seq DESC LIMIT 1", (ref,)
        ).fetchone()
        a = prev["score_a"] if prev else row["start_a"]
        b = prev["score_b"] if prev else row["start_b"]
        db.execute(
            "UPDATE score_games SET score_a = ?, score_b = ? WHERE ref = ?",
            (a, b, ref),
        )
        _bump_rev(db, ref)
        sides = db.execute(
            "SELECT team_a, team_b FROM matches WHERE id = ?", (row["match_id"],)
        ).fetchone()
        serve = (
            _serve_hint(db, ref, sides["team_a"], sides["team_b"])
            if sides else {"team": "", "side": "", "streak": False, "text": ""}
        )
        return {
            "ok": True, "ref": ref, "score_a": a, "score_b": b,
            "state": "running", "need_switch": False, "reached_end": False,
            "target": row["target"], "switch_at": row["switch_at"],
            "serve": serve,
        }


def mark_switched(ref: str) -> bool:
    """裁判确认换边后调用：只标记一次，界面左右对调由前端完成。"""
    with _score_lock, connection() as db:
        cur = db.execute(
            "UPDATE score_games SET switched = 1 WHERE ref = ? AND switched = 0",
            (ref,),
        )
        if cur.rowcount:
            _bump_rev(db, ref)
    return True


def finish_score_game(ref: str) -> dict:
    """裁判确认本局结束：必须有一方累计分达到本局目标分。

    只锁定该局、不写胜方；下一局开赛时会读取这里的累计比分继续追分。
    """
    with _score_lock, connection() as db:
        row = db.execute(
            "SELECT * FROM score_games WHERE ref = ?", (ref,)
        ).fetchone()
        if not row or row["state"] != "running":
            return {"ok": False, "error": "这一局不在进行中"}
        rule = game_rule(row["game_no"])
        target = row["target"] or rule["target"]
        if max(row["score_a"], row["score_b"]) < target:
            return {"ok": False, "error": "尚未有一方达到 %d 分" % target}
        db.execute(
            "UPDATE score_games SET state = 'finished', finished_at = ? WHERE ref = ?",
            (_now(), ref),
        )
        _bump_rev(db, ref)
        next_ref = None
        if row["game_no"] < len(GAMES):
            next_ref = "%d-%d" % (row["match_id"], row["game_no"] + 1)
        return {
            "ok": True, "ref": ref, "state": "finished",
            "score_a": row["score_a"], "score_b": row["score_b"],
            "game_no": row["game_no"], "target": target,
            "next_ref": next_ref,
        }


def live_payload(viewer_name: str = "") -> dict[str, dict]:
    """/api/live 的数据源：返回所有已开赛局的展示数据。

    前端按 v（rev）比对，只 patch 发生变化的卡片，不整页 reload。
    竞猜比例对所有用户可见，个人预计得分与结算仅返回当前用户的数据。
    """
    games = all_score_games()
    if not games:
        return {}
    matches = {m["id"]: m for m in all_matches()}
    with connection() as db:
        ev_rows = db.execute(
            "SELECT ref, seq, scoring_team, score_a, score_b FROM score_events "
            "ORDER BY ref, seq"
        ).fetchall()
    events: dict[str, list] = {}
    for row in ev_rows:
        events.setdefault(row["ref"], []).append({
            "team": row["scoring_team"],
            "a": row["score_a"], "b": row["score_b"],
        })
    predictions = guess_results(viewer_name)
    result = {}
    for ref, g in games.items():
        m = matches.get(g["match_id"])
        if not m:
            continue
        rule = game_rule(g["game_no"])
        result[ref] = {
            "v": g["rev"], "ref": ref, "state": g["state"],
            "match_id": g["match_id"], "game_no": g["game_no"],
            "team_a": m["team_a"], "team_b": m["team_b"],
            "score_a": g["score_a"], "score_b": g["score_b"],
            "start_a": g["start_a"], "start_b": g["start_b"],
            # 本局得分 = 当前累计分 − 本局承接分（追分制，绝不跨局归零）。
            # 双打两人共享同一分数；前端在选手名字旁标注。
            "gain_a": max(0, g["score_a"] - (g["start_a"] or 0)),
            "gain_b": max(0, g["score_b"] - (g["start_b"] or 0)),
            "target": g["target"] or rule["target"],
            "switch_at": g["switch_at"] or rule["switch_at"],
            "switched": g["switched"],
            "court_input": g["court_input"], "referee": g["referee"],
            "rallies": events.get(ref, []),
            "predict": predictions.get(ref, {}).get("predict"),
            "expected": predictions.get(ref, {}).get("expected"),
            "my_pick": predictions.get(ref, {}).get("my_pick"),
            "settled": predictions.get(ref, {}).get("settled"),
        }
    return result


def custom_live_payload() -> dict[str, dict]:
    """/api/live 的自定义对局增量数据，键为 'c' + 对局 id，避免与正式局 ref 冲突。

    字段刻意与 live_payload() 对齐（ref/team/target/score/gain/rallies/...），
    并在开头带 custom=True，使赛程页 live.js 能用同一套渲染管线实时展示，
    并从 0:0 起算本局得分（自定义对局不从上一局承接比分）。
    """
    with connection() as db:
        rows = db.execute(
            "SELECT id, team_a, team_b, event, players_a, players_b, target, "
            "win_by_two, state, score_a, score_b, winner, referee "
            "FROM custom_matches ORDER BY id"
        ).fetchall()
        ev = db.execute(
            "SELECT match_id, scoring_team, score_a, score_b "
            "FROM custom_match_events ORDER BY match_id, seq"
        ).fetchall()
    events: dict[int, list] = {}
    for r in ev:
        events.setdefault(r["match_id"], []).append({
            "team": r["scoring_team"], "a": r["score_a"], "b": r["score_b"],
        })
    result: dict[str, dict] = {}
    for r in rows:
        key = "c%d" % r["id"]
        rallies = events.get(r["id"], [])
        result[key] = {
            "v": len(rallies), "ref": key, "custom": True,
            "id": r["id"], "state": r["state"],
            "team_a": r["team_a"], "team_b": r["team_b"],
            "event": r["event"], "target": r["target"],
            "win_by_two": bool(r["win_by_two"]), "winner": r["winner"] or "",
            # 待确认：比分已达标但裁判尚未点确认，榜单此时还未计入。
            "pending": bool(r["winner"]) and r["state"] == "running",
            "score_a": r["score_a"], "score_b": r["score_b"],
            # 自定义对局从 0:0 开始，不存在承接分，本局得分即当前分。
            "start_a": 0, "start_b": 0,
            "gain_a": r["score_a"], "gain_b": r["score_b"],
            "switch_at": 0, "switched": 0,
            "referee": r["referee"], "court_input": "",
            "players_a": _split_players(r["players_a"]),
            "players_b": _split_players(r["players_b"]),
            "rallies": rallies,
        }
    return result


def match_results() -> dict[int, dict]:
    """按场次汇总三态、当前累计比分与净胜分，供赛程页场次卡片实时展示。

      pending  —— 一局都没开赛，不显示状态标签
      running  —— 已开赛但 6 局未全部结束，绿色「正在进行中」
      finished —— 6 局全部结束，金色「已完赛」

    text 是「当前累计比分」（如 15:14），不再有胜局数或 +1。
    净胜分只统计 6 局全部结束的场次，用第 6 局结束时的累计比分相减。
    """
    by_match: dict[int, list[dict]] = {}
    for g in all_score_games().values():
        by_match.setdefault(g["match_id"], []).append(g)
    required = {g["no"] for g in GAMES}
    last_no = GAMES[-1]["no"]
    result = {}
    for m in all_matches():
        mid = m["id"]
        rows = by_match.get(mid, [])
        done_by_no = {
            g["game_no"]: g for g in rows if g["state"] == "finished"
        }
        finished = required.issubset(done_by_no.keys())
        started = bool(rows)
        state = "finished" if finished else ("running" if started else "pending")
        current = max(rows, key=lambda g: (g["game_no"], g["rev"])) if rows else None
        a_score = current["score_a"] if current else 0
        b_score = current["score_b"] if current else 0
        running_no = next(
            (g["game_no"] for g in rows if g["state"] == "running"), None
        )
        sixth = done_by_no.get(last_no)
        final_a = sixth["score_a"] if sixth else None
        final_b = sixth["score_b"] if sixth else None
        net = (final_a - final_b) if finished and sixth else None
        info = {
            "finished": finished, "state": state,
            "v": sum(int(g.get("rev") or 0) for g in rows),
            "team_a": m["team_a"], "team_b": m["team_b"],
            "text": "%d:%d" % (a_score, b_score),
            "score_a": a_score, "score_b": b_score,
            "final_a": final_a, "final_b": final_b, "net": net,
            "done_games": len(done_by_no),
            "running_no": running_no,
            "current_target": game_rule(running_no)["target"] if running_no else None,
            "title": "",
        }
        if state == "finished":
            info["title"] = (
                "%s 组 %d : %d %s 组｜最终累计比分，净胜分 %+d；"
                "6 局比分全程继承，逐局目标 15/30/45/60/75/90"
                % (m["team_a"], final_a, final_b, m["team_b"], net)
            )
        elif state == "running":
            info["title"] = (
                "%s 组 %d : %d %s 组｜已结束 %d/%d 局，当前累计比分；"
                "第 %s 局目标 %s 分"
                % (m["team_a"], a_score, b_score, m["team_b"],
                   len(done_by_no), len(GAMES),
                   running_no if running_no else "—",
                   info["current_target"] if info["current_target"] else "—")
            )
        result[mid] = info
    return result


def _longest_streak(seq: list[str], team: str) -> int:
    """单局内某组连续得分的最长段；不同局各自独立（换局清零）。"""
    best = cur = 0
    for t in seq:
        if t == team:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


# ----------------------------- 自定义对局 -----------------------------
def _split_players(raw: str) -> list[str]:
    """上场人员列是把姓名用 '|' 连接后入库，读时拆回列表。"""
    return [n for n in (raw or "").split("|") if n]


def create_custom_match(team_a: str, team_b: str, event: str,
                        players_a: list[str], players_b: list[str],
                        target: int, win_by_two: bool, referee: str):
    """裁判台发起一场自定义对局，从 0:0 开始（state=running）。
    校验：两组不同且合法、项目合法、人数匹配、人员属于各自组、组内不重复，
    混双同样要求 1 男 1 女。返回 (match_id, message) 或 (None, error)。
    """
    errors = []
    if team_a not in ROSTER or team_b not in ROSTER:
        errors.append("组别无效")
    elif team_a == team_b:
        errors.append("两个小组不能相同")
    meta = next((e for e in CUSTOM_EVENTS if e["event"] == event), None)
    if not meta:
        errors.append("对局类型无效")
    pa = [str(n).strip() for n in players_a if str(n).strip()]
    pb = [str(n).strip() for n in players_b if str(n).strip()]
    if meta and len(pa) != meta["size"]:
        errors.append(f'{event} {meta["size"]} 人，A 组上场人数不符')
    if meta and len(pb) != meta["size"]:
        errors.append(f'{event} {meta["size"]} 人，B 组上场人数不符')
    if len(set(pa)) != len(pa) or len(set(pb)) != len(pb):
        errors.append("同一方上场人员不能重复")
    try:
        target = int(target)
    except (TypeError, ValueError):
        target = 0
    if target < 1 or target > 200:
        errors.append("目标分需在 1–200 之间")
    if errors:
        return None, "；".join(errors)

    gender = {m["name"]: m["gender"] for m in team_members(team_a)}
    gender.update({m["name"]: m["gender"] for m in team_members(team_b)})
    for team, names in ((team_a, pa), (team_b, pb)):
        roster = {m["name"] for m in team_members(team)}
        for name in names:
            if name not in roster:
                errors.append(f"{name} 不在 {team} 组名单中")
    if event == "混双" and not errors:
        for names in (pa, pb):
            if {gender.get(n) for n in names} != {"男", "女"}:
                errors.append("混双须 1 男 1 女")
                break
    if errors:
        return None, "；".join(errors)

    with connection() as db:
        cur = db.execute(
            "INSERT INTO custom_matches "
            "(team_a, team_b, event, players_a, players_b, target, win_by_two, "
            " referee, created_at, state, score_a, score_b) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 0, 0)",
            (team_a, team_b, event, "|".join(pa), "|".join(pb),
             target, 1 if win_by_two else 0, referee, _now()),
        )
        match_id = cur.lastrowid
    return match_id, "已创建自定义对局"


def custom_matches_all() -> list[dict]:
    """全部自定义对局（新创建的在前），供赛程页与裁判台列表展示。

    附带 players_*_list，供统一记分面板复用；本局得分即当前分（从 0:0 起）。
    """
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM custom_matches ORDER BY id DESC"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["players_a_list"] = _split_players(d.get("players_a", ""))
        d["players_b_list"] = _split_players(d.get("players_b", ""))
        d["win_by_two"] = bool(d.get("win_by_two"))
        d["gain_a"] = d["score_a"]
        d["gain_b"] = d["score_b"]
        d["ref"] = "c%d" % d["id"]
        out.append(d)
    return out


def custom_match_detail(match_id: int) -> dict | None:
    with connection() as db:
        row = db.execute(
            "SELECT * FROM custom_matches WHERE id = ?", (match_id,)
        ).fetchone()
        if not row:
            return None
        events = db.execute(
            "SELECT scoring_team FROM custom_match_events WHERE match_id = ? ORDER BY seq",
            (match_id,),
        ).fetchall()
    d = dict(row)
    seq = [e["scoring_team"] for e in events]
    d["players_a_list"] = _split_players(d.get("players_a", ""))
    d["players_b_list"] = _split_players(d.get("players_b", ""))
    d["streak_a"] = _longest_streak(seq, d["team_a"])
    d["streak_b"] = _longest_streak(seq, d["team_b"])
    d["rallies"] = [
        {"team": team, "a": sa, "b": sb}
        for _seq, team, sa, sb in events_scores(match_id)
    ]
    return d


def events_scores(match_id: int):
    """按 seq 返回 (seq, scoring_team, score_a, score_b)。"""
    with connection() as db:
        rows = db.execute(
            "SELECT seq, scoring_team, score_a, score_b FROM custom_match_events "
            "WHERE match_id = ? ORDER BY seq", (match_id,)
        ).fetchall()
    return [(r["seq"], r["scoring_team"], r["score_a"], r["score_b"]) for r in rows]


def _custom_side_won(sa: int, sb: int, target: int, win_by_two: bool) -> str | None:
    """按项目规则判断当前比分是否出现胜方，返回 'a' / 'b' / None。"""
    if win_by_two:
        if sa >= target and sa - sb >= 2:
            return "a"
        if sb >= target and sb - sa >= 2:
            return "b"
    else:
        if sa >= target:
            return "a"
        if sb >= target:
            return "b"
    return None


def custom_add_point(match_id: int, team: str):
    """给自定义对局加一分；达到目标分只标记「待确认」，不直接结束。

    候选胜方暂存在 winner，state 仍为 running，所以榜单不会提前计入；
    裁判点「返回修改比分」后可继续回退。只有 custom_finish() 确认后才结束。
    返回 dict(ok, error, state, score_a, score_b, reached_end, target, v)。
    """
    with connection() as db:
        match = db.execute(
            "SELECT * FROM custom_matches WHERE id = ?", (match_id,)
        ).fetchone()
        if not match:
            return {"ok": False, "error": "对局不存在"}
        if match["state"] != "running":
            return {"ok": False, "error": "该对局已结束"}
        if team not in (match["team_a"], match["team_b"]):
            return {"ok": False, "error": "无效的组别"}
        last = db.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM custom_match_events WHERE match_id = ?",
            (match_id,),
        ).fetchone()["m"]
        sa = match["score_a"] + (1 if team == match["team_a"] else 0)
        sb = match["score_b"] + (1 if team == match["team_b"] else 0)
        db.execute(
            "INSERT INTO custom_match_events "
            "(match_id, seq, scoring_team, score_a, score_b, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (match_id, last + 1, team, sa, sb, _now()),
        )
        side = _custom_side_won(sa, sb, match["target"], bool(match["win_by_two"]))
        winner = (match["team_a"] if side == "a"
                  else match["team_b"] if side == "b" else None)
        db.execute(
            "UPDATE custom_matches SET score_a = ?, score_b = ?, winner = ? WHERE id = ?",
            (sa, sb, winner, match_id),
        )
    return {
        "ok": True, "error": "", "state": "running",
        "score_a": sa, "score_b": sb,
        "reached_end": bool(winner), "target": match["target"],
        "pending_winner": winner or "", "winner": "", "v": last + 1,
    }


def custom_finish(match_id: int):
    """二次确认自定义对局结果：复核当前比分达标后才结束并计入榜单。"""
    with connection() as db:
        match = db.execute(
            "SELECT * FROM custom_matches WHERE id = ?", (match_id,)
        ).fetchone()
        if not match:
            return {"ok": False, "error": "对局不存在"}
        score_a, score_b = match["score_a"], match["score_b"]
        if match["state"] == "finished":
            return {"ok": True, "error": "", "state": "finished",
                    "score_a": score_a, "score_b": score_b,
                    "winner": match["winner"] or ""}
        if match["state"] != "running":
            return {"ok": False, "error": "该对局不在进行中"}
        side = _custom_side_won(
            score_a, score_b, match["target"], bool(match["win_by_two"])
        )
        if not side:
            return {"ok": False, "error": "当前比分尚未达到结束条件，请先回退或继续记分"}
        winner = match["team_a"] if side == "a" else match["team_b"]
        db.execute(
            "UPDATE custom_matches SET state = 'finished', winner = ?, finished_at = ? "
            "WHERE id = ?",
            (winner, _now(), match_id),
        )
    return {"ok": True, "error": "", "state": "finished",
            "score_a": score_a, "score_b": score_b, "winner": winner}


def custom_undo_point(match_id: int):
    """撤销自定义对局最后一分（已结束后也可撤回重记）。"""
    with connection() as db:
        match = db.execute(
            "SELECT * FROM custom_matches WHERE id = ?", (match_id,)
        ).fetchone()
        if not match:
            return {"ok": False, "error": "对局不存在"}
        last = db.execute(
            "SELECT * FROM custom_match_events WHERE match_id = ? ORDER BY seq DESC LIMIT 1",
            (match_id,),
        ).fetchone()
        if not last:
            return {"ok": False, "error": "没有可撤销的得分"}
        db.execute("DELETE FROM custom_match_events WHERE id = ?", (last["id"],))
        prev = db.execute(
            "SELECT score_a, score_b FROM custom_match_events "
            "WHERE match_id = ? ORDER BY seq DESC LIMIT 1", (match_id,)
        ).fetchone()
        sa = prev["score_a"] if prev else 0
        sb = prev["score_b"] if prev else 0
        db.execute(
            "UPDATE custom_matches SET score_a = ?, score_b = ?, "
            "state = 'running', winner = NULL, finished_at = NULL WHERE id = ?",
            (sa, sb, match_id),
        )
    return {"ok": True, "error": "", "state": "running", "reached_end": False,
            "score_a": sa, "score_b": sb, "v": last["seq"] - 1}


def custom_delete(match_id: int):
    """删除一场自定义对局（含逐分流水），用于误建纠正。"""
    with connection() as db:
        db.execute("DELETE FROM custom_match_events WHERE match_id = ?", (match_id,))
        db.execute("DELETE FROM custom_matches WHERE id = ?", (match_id,))


# ----------------------------- 积分榜单 -----------------------------
def guess_rank_key(row: dict) -> tuple:
    """总积分降序、单局最高分降序、猜中次数升序、总竞猜次数升序、姓名。"""
    return (-row["score"], -row["max_gain"], row["hits"], row["bets"], row["name"])


def _guess_leaderboard(games=None, matches=None) -> list[dict]:
    """小局积分和最高单次得分共用结算公式；总竞猜次数含小局和冠军选择。"""
    if games is None:
        games = all_score_games()
    if matches is None:
        matches = {match["id"]: match for match in all_matches()}
    outcomes = guess_results()
    # 竞猜记分：猜中按「基础分 + 对方组押注分数 / 己方押注人数」，猜错 0 分
    with connection() as db:
        guess_rows = db.execute(
            "SELECT name, kind, ref, pick, stake FROM guesses"
        ).fetchall()
        player_names = {r["name"] for r in db.execute("SELECT name FROM players")}
        teacher_names = {r["name"] for r in db.execute("SELECT name FROM teachers")}
    by_ref: dict[str, dict[str, dict]] = {}
    for r in guess_rows:
        if r["kind"] != "game":
            continue
        g = games.get(r["ref"])
        if not g or g["state"] != "finished":
            continue
        by_ref.setdefault(r["ref"], {}).setdefault(
            r["pick"], {"count": 0, "stake": 0}
        )
        by_ref[r["ref"]][r["pick"]]["count"] += 1
        by_ref[r["ref"]][r["pick"]]["stake"] += r["stake"]

    guess_stat: dict[str, dict] = {}
    for r in guess_rows:
        st = guess_stat.setdefault(r["name"], {
            "name": r["name"], "score": 0.0, "hits": 0, "bets": 0,
            "max_gain": 0.0, "game_bets": 0,
        })
        # 有竞猜记录就上榜：未开赛 / 未结算的显示 0.00 分
        st["bets"] += 1
        if r["kind"] != "game":
            continue
        st["game_bets"] += 1
        g = games.get(r["ref"])
        if not g or g["state"] != "finished":
            continue
        if r["pick"] != outcomes.get(r["ref"], {}).get("winner"):
            continue
        m = matches.get(g["match_id"])
        if not m:
            continue
        stats = by_ref.get(r["ref"], {})
        own = stats.get(r["pick"], {"count": 0, "stake": 0})
        other_team = m["team_b"] if r["pick"] == m["team_a"] else m["team_a"]
        other = stats.get(other_team, {"count": 0, "stake": 0})
        gain = guess_gain(r["stake"], own["count"], other["stake"])
        st["score"] += gain
        st["max_gain"] = max(st["max_gain"], gain)
        st["hits"] += 1
    guess_rows_out = []
    for st in guess_stat.values():
        guess_rows_out.append({
            **st,
            "score_text": f"{st['score']:.2f}",
            "max_gain_text": f"{st['max_gain']:.2f}",
            "is_spectator": st["name"] not in player_names
                            and st["name"] not in teacher_names,
        })
    guess_rows_out.sort(key=guess_rank_key)
    return guess_rows_out


def standings() -> dict:
    """三张榜的实时聚合，由 score_games / score_events / guesses 派生。

    组别榜：先按胜场排名，胜场相同再按净胜分（净胜分 = 与三个对手组打完后的
    累计比分差之和，只统计 6 局全部结束的场次），胜场与净胜分都相同才并列
    同名次；胜场 = 逐分流水里整场先到 90 分的一方记 1 胜，同样只统计完赛场次。
    已结束的自定义对局额外记 1 胜（胜方）、把最终比分差计入对应两组净胜分，
    并给双方各记 1 个已完赛场次；总场次也从 6 场扩展为「6 + 自定义对局总数」。
    选手榜：得分 = 参与的已结束小局中己方本局增量得分之和（双打两人共享）；
    最高连得 = 按逐分流水算、单局内己方连续得分的最长段，换局清零，取最大值。
    自定义对局按上场人员把本方最终得分计入选手得分、参与局数 +1、连得并入最高连得。
    """
    with connection() as conn:
        final = conn.execute("SELECT snapshot FROM tournament_result WHERE id = 1").fetchone()
    if final:
        board = json.loads(final["snapshot"])
        # 兼容升级前的最终榜单：仅补充新统计，保留已结算的积分和猜中次数。
        fields = ("max_gain", "max_gain_text", "game_bets")
        if any(any(field not in row for field in fields) for row in board["guesses"]):
            history = {row["name"]: row for row in _guess_leaderboard()}
            for row in board["guesses"]:
                stats = history.get(row["name"], {"max_gain": 0.0, "max_gain_text": "0.00", "game_bets": 0})
                for field in fields:
                    row.setdefault(field, stats[field])
        board["guesses"].sort(key=guess_rank_key)
        return board
    games = all_score_games()
    matches = {m["id"]: m for m in all_matches()}
    # 按「场次 + 组」取实际阵容：开赛时已固化，后续改安排不影响已结束局的选手归属。
    lineups = all_effective_lineups()
    results = match_results()

    with connection() as db:
        ev_rows = db.execute(
            "SELECT ref, seq, scoring_team, score_a, score_b "
            "FROM score_events ORDER BY ref, seq"
        ).fetchall()
    by_ref: dict[str, list[str]] = {}
    match_events: dict[int, list[tuple[int, int, int, int]]] = {}
    for r in ev_rows:
        by_ref.setdefault(r["ref"], []).append(r["scoring_team"])
        if not r["ref"] or "-" not in r["ref"]:
            continue
        try:
            mid_text, gno_text = r["ref"].split("-", 1)
            mid_no, gno_no = int(mid_text), int(gno_text)
        except ValueError:
            continue
        match_events.setdefault(mid_no, []).append(
            (gno_no, int(r["seq"] or 0), int(r["score_a"] or 0), int(r["score_b"] or 0))
        )
    for events in match_events.values():
        events.sort()

    team_stat = {
        t: {"team": t, "net": 0, "wins": 0, "matches": 0} for t in ROSTER
    }
    final_target = game_rule(len(GAMES))["target"]
    for mid, outcome in results.items():
        if not outcome["finished"]:
            continue
        m = matches.get(mid)
        if not m:
            continue
        a, b = m["team_a"], m["team_b"]
        diff = outcome["final_a"] - outcome["final_b"]
        team_stat[a]["net"] += diff
        team_stat[b]["net"] -= diff
        team_stat[a]["matches"] += 1
        team_stat[b]["matches"] += 1
        # 整场胜方 = 逐分流水中第一次累计到 90 分的一方；
        # 若历史数据缺流水，回退到最终比分高者，避免漏记胜场。
        winner = None
        for _gno, _seq, score_a, score_b in match_events.get(mid, []):
            if score_a >= final_target or score_b >= final_target:
                winner = a if score_a >= final_target else b
                break
        if winner is None:
            winner = a if diff > 0 else b if diff < 0 else None
        if winner:
            team_stat[winner]["wins"] += 1

    # 自定义对局：额外胜场 + 净胜分 + 双方各 1 个已完赛场次（进行中的算总场次）
    with connection() as db:
        custom_rows = db.execute(
            "SELECT * FROM custom_matches WHERE state = 'finished'"
        ).fetchall()
        custom_total = db.execute(
            "SELECT COUNT(*) AS n FROM custom_matches"
        ).fetchone()["n"]
        custom_ev = db.execute(
            "SELECT match_id, scoring_team FROM custom_match_events ORDER BY match_id, seq"
        ).fetchall()
    custom_seq: dict[int, list[str]] = {}
    for r in custom_ev:
        custom_seq.setdefault(r["match_id"], []).append(r["scoring_team"])
    for cm in custom_rows:
        a, b = cm["team_a"], cm["team_b"]
        if a not in team_stat or b not in team_stat:
            continue
        sa, sb = cm["score_a"], cm["score_b"]
        team_stat[a]["net"] += sa - sb
        team_stat[b]["net"] -= sa - sb
        team_stat[a]["matches"] += 1
        team_stat[b]["matches"] += 1
        if cm["winner"] in team_stat:
            team_stat[cm["winner"]]["wins"] += 1

    team_rows = sorted(
        team_stat.values(), key=lambda x: (-x["wins"], -x["net"], x["team"])
    )
    rank, prev_key = 0, None
    for i, row in enumerate(team_rows, 1):
        cur_key = (row["wins"], row["net"])
        if cur_key != prev_key:
            rank, prev_key = i, cur_key
        row["rank"] = rank
        row["net_text"] = "%+d" % row["net"]

    player_stat: dict[str, dict] = {}
    for g in games.values():
        if g["state"] != "finished":
            continue
        m = matches.get(g["match_id"])
        if not m:
            continue
        a, b = m["team_a"], m["team_b"]
        seq = by_ref.get(g["ref"], [])
        for team, gain in (
            (a, g["score_a"] - (g["start_a"] or 0)),
            (b, g["score_b"] - (g["start_b"] or 0)),
        ):
            streak = _longest_streak(seq, team)
            row = lineups.get((g["match_id"], team), {}).get(g["game_no"], {})
            names = [n for n in (row.get("player1"), row.get("player2")) if n]
            for name in names:
                st = player_stat.setdefault(name, {
                    "name": name, "team": team,
                    "games": 0, "points": 0, "best_streak": 0,
                })
                st["games"] += 1
                st["points"] += gain
                st["best_streak"] = max(st["best_streak"], streak)

    # 自定义对局的上场选手：把本方最终得分计入选手积分、参与局数 +1、连得并入最高连得。
    for cm in custom_rows:
        seqs = custom_seq.get(cm["id"], [])
        for team, score, names in (
            (cm["team_a"], cm["score_a"], _split_players(cm["players_a"])),
            (cm["team_b"], cm["score_b"], _split_players(cm["players_b"])),
        ):
            streak = _longest_streak(seqs, team)
            for name in names:
                st = player_stat.setdefault(name, {
                    "name": name, "team": team,
                    "games": 0, "points": 0, "best_streak": 0,
                })
                st["games"] += 1
                st["points"] += score
                st["best_streak"] = max(st["best_streak"], streak)

    player_rows = sorted(
        player_stat.values(),
        key=lambda x: (-x["points"], -x["best_streak"], -x["games"], x["name"]),
    )
    # 冠军竞猜仅在四位队长确认并保存最终榜单时结算。
    champion = None
    guess_rows_out = _guess_leaderboard(games, matches)
    return {
        "teams": team_rows,
        "players": player_rows,
        "guesses": guess_rows_out,
        "champion": champion,
        "finished_games": sum(1 for g in games.values() if g["state"] == "finished"),
        "total_games": len(matches) * len(GAMES),
        # 已完赛场次含自定义对局；总场次 = 6 场正式 + 所有已创建的自定义对局。
        "finished_matches": (sum(1 for o in results.values() if o["finished"])
                             + len(custom_rows)),
        "total_matches": len(matches) + custom_total,
    }


# ----------------------------- 四队长确认结束 -----------------------------
def _tournament_fingerprint(conn) -> str:
    tables = ("score_games", "score_events", "custom_matches", "custom_match_events",
              "players", "team_lineups", "match_lineups")
    state = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
             for table in tables}
    return hashlib.sha256(json.dumps(state, ensure_ascii=False).encode("utf-8")).hexdigest()


def tournament_is_finalized() -> bool:
    with connection() as conn:
        return conn.execute("SELECT 1 FROM tournament_result WHERE id = 1").fetchone() is not None


def tournament_status(viewer_name: str = "") -> dict:
    with connection() as conn:
        final = conn.execute("SELECT * FROM tournament_result WHERE id = 1").fetchone()
        dismissed = bool(final and conn.execute(
            "SELECT 1 FROM tournament_champion_dismissals WHERE name = ? AND finalized_at = ?",
            (viewer_name, final["finalized_at"]),
        ).fetchone())
        fingerprint = _tournament_fingerprint(conn) if not final else None
        confirmations = [dict(row) for row in conn.execute(
            "SELECT team, captain, confirmed_at FROM tournament_confirmations "
            + ("WHERE fingerprint = ? " if not final else "") + "ORDER BY team",
            (fingerprint,) if not final else (),
        )]
    board = json.loads(final["snapshot"]) if final else {}
    return {"finalized": bool(final), "finalized_at": final["finalized_at"] if final else None,
            "celebration_dismissed": dismissed,
            "confirmations": confirmations, "confirmed_teams": [r["team"] for r in confirmations],
            "confirmed_count": len(confirmations), "teams": list(ROSTER),
            "champions": board.get("champions", []),
            "champion_rosters": board.get("champion_rosters", {})}


def dismiss_champion_celebration(name: str, finalized_at: str) -> bool:
    with connection() as conn:
        final = conn.execute("SELECT finalized_at FROM tournament_result WHERE id = 1").fetchone()
        if not final or finalized_at != final["finalized_at"]:
            return False
        conn.execute("INSERT OR IGNORE INTO tournament_champion_dismissals VALUES (?, ?, ?)",
                     (name, finalized_at, _now()))
    return True


def confirm_tournament_end(team: str, captain: str) -> dict:
    """同一榜单版本需四组各确认一次，最后一次确认原子保存排名与竞猜结算快照。"""
    with _score_lock, connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        authorized = conn.execute(
            "SELECT 1 FROM players WHERE name = ? AND team = ? AND is_captain = 1",
            (captain, team),
        ).fetchone()
        if team not in ROSTER or not authorized:
            return {"ok": False, "error": "仅本组队长可确认比赛结束"}
        if conn.execute("SELECT 1 FROM tournament_result WHERE id = 1").fetchone():
            return {"ok": True, "finalized": True}
        running = conn.execute("SELECT 1 FROM score_games WHERE state = 'running' UNION ALL "
                               "SELECT 1 FROM custom_matches WHERE state = 'running' LIMIT 1").fetchone()
        if running:
            return {"ok": False, "error": "仍有进行中的对局，请裁判确认结束后再确认比赛结束"}
        fingerprint = _tournament_fingerprint(conn)
        conn.execute("DELETE FROM tournament_confirmations WHERE fingerprint <> ?", (fingerprint,))
        conn.execute("INSERT OR IGNORE INTO tournament_confirmations VALUES (?, ?, ?, ?)",
                     (team, captain, _now(), fingerprint))
        confirmed = {row["team"] for row in conn.execute("SELECT team FROM tournament_confirmations")}
        finalized = set(ROSTER).issubset(confirmed)
        if finalized:
            board = standings()
            champions = [row["team"] for row in board["teams"] if row["rank"] == 1]
            board["champions"] = champions
            board["champion"] = champions[0] if len(champions) == 1 else None
            board["champion_rosters"] = {
                t: [r["name"] for r in conn.execute("SELECT name FROM players WHERE team = ? ORDER BY sort_order, name", (t,))]
                for t in champions
            }
            winners = {row["name"] for row in conn.execute("SELECT name, pick FROM guesses WHERE kind = 'champion'")
                       if row["pick"] in champions}
            for row in board["guesses"]:
                if row["name"] in winners:
                    row["score"] += 5.0
                    row["hits"] += 1
                    row["score_text"] = f"{row['score']:.2f}"
            board["guesses"].sort(key=guess_rank_key)
            conn.execute("INSERT INTO tournament_result VALUES (1, ?, ?)",
                         (_now(), json.dumps(board, ensure_ascii=False)))
        return {"ok": True, "finalized": finalized, "confirmed_count": len(confirmed)}
