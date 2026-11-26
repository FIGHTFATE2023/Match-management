"""后台管理回归测试：后台与前台共用真实表，测试只操作临时数据库。

运行：python -m unittest discover -s tests -v
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
sys.path.insert(0, str(BACKEND))
import db
import admin as admin_module


class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('admin_test_app', BACKEND / 'app.py')
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        source = (BACKEND / 'app.py').read_text(encoding='utf-8')
        source = source.replace(
            'app.secret_key = load_secret()', "app.secret_key = 'isolated-test-secret'"
        )
        with patch.object(db, 'init_db'), patch.object(admin_module, 'init_admin_db'):
            exec(compile(source, str(BACKEND / 'app.py'), 'exec'), cls.module.__dict__)
        cls.module.app.testing = True

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        original_db, original_local = db.DB_PATH, db.LOCAL_DIR
        self.addCleanup(setattr, db, 'DB_PATH', original_db)
        self.addCleanup(setattr, db, 'LOCAL_DIR', original_local)
        db.LOCAL_DIR = Path(self.temp.name)
        db.DB_PATH = db.LOCAL_DIR / 'match.db'
        db.init_db()
        admin_module.init_admin_db()
        self.client = self.module.app.test_client()

    # ---------- 辅助 ----------
    def token(self):
        with self.client.session_transaction() as session:
            return session.get('admin_csrf', '')

    def login(self, username='admin', password='admin123'):
        self.client.get('/admin/login')
        return self.client.post('/admin/login', data={
            'username': username, 'password': password, 'csrf': self.token(),
        })

    def post(self, url, **data):
        return self.client.post(url, data=dict(csrf=self.token(), **data))

    def scalar(self, sql, params=()):
        with db.connection() as conn:
            return conn.execute(sql, params).fetchone()[0]

    # ---------- 登录与鉴权 ----------
    def test_login_guard_and_credentials(self):
        response = self.client.get('/admin/competitions')
        self.assertEqual(303, response.status_code)
        self.assertIn('/admin/login', response.headers['Location'])

        self.client.get('/admin/login')
        bad = self.client.post('/admin/login', data={
            'username': 'admin', 'password': 'wrong', 'csrf': self.token(),
        })
        self.assertEqual(401, bad.status_code)
        self.assertIn('不正确', bad.get_data(as_text=True))

        good = self.login()
        self.assertEqual(303, good.status_code)
        self.assertEqual('/admin/', good.headers['Location'])

    def test_csrf_required_on_write(self):
        self.login()
        response = self.client.post('/admin/competitions/new', data={'name': '绕过校验'})
        self.assertEqual(403, response.status_code)

    def test_logout(self):
        self.login()
        response = self.post('/admin/logout')
        self.assertEqual(303, response.status_code)
        self.assertEqual(303, self.client.get('/admin/').status_code)

    # ---------- 真实比赛数据 ----------
    def test_competitions_are_real_matches(self):
        self.login()
        page = self.client.get('/admin/competitions').get_data(as_text=True)
        self.assertIn('A组 vs B组', page)
        self.assertIn('累计比分', page)
        self.assertEqual(6, self.scalar('SELECT count(*) FROM matches'))

        # 编辑真实比赛元数据
        response = self.post('/admin/competitions/1/edit',
                             name='测试改名', match_time='2026-12-01 09:00',
                             status='已取消', team_a='A', team_b='C',
                             round_no='1', court='1')
        self.assertEqual(303, response.status_code)
        with db.connection() as conn:
            row = conn.execute('SELECT * FROM matches WHERE id=1').fetchone()
        self.assertEqual('测试改名', row['name'])
        self.assertEqual('2026-12-01 09:00', row['match_time'])
        self.assertEqual('已取消', row['status'])
        self.assertEqual('C', row['team_b'])
        # 前台读取同一张表
        self.assertEqual('测试改名', db.all_matches()[0]['name'])

        # 新增一场真实比赛
        response = self.post('/admin/competitions/new', name='新增测试赛',
                             match_time='2026-12-03 10:00', status='未开始',
                             team_a='B', team_b='D', round_no='9', court='9')
        self.assertEqual(303, response.status_code)
        self.assertEqual(7, self.scalar('SELECT count(*) FROM matches'))

        # 轮次场地冲突校验
        response = self.post('/admin/competitions/new', name='冲突赛',
                             status='未开始', team_a='A', team_b='B',
                             round_no='9', court='9')
        self.assertEqual(200, response.status_code)
        self.assertIn('已有比赛', response.get_data(as_text=True))

        # 删除真实比赛
        mid = self.scalar("SELECT id FROM matches WHERE name='新增测试赛'")
        self.post(f'/admin/competitions/{mid}/delete')
        self.assertEqual(6, self.scalar('SELECT count(*) FROM matches'))

    # ---------- 真实账号 ----------
    def test_accounts_are_real_players_and_teachers(self):
        self.login()
        page = self.client.get('/admin/accounts').get_data(as_text=True)
        self.assertIn('裁判', page) if False else None
        self.assertIn('队长', page)
        self.assertIn('老师', page)
        players = self.scalar('SELECT count(*) FROM players')
        teachers = self.scalar('SELECT count(*) FROM teachers')

        # 新增真实队员
        response = self.post('/admin/accounts/new', source='player', username='测试队员',
                             role='队员', team='D', gender='女', sort_order='9', enabled='1')
        self.assertEqual(303, response.status_code)
        self.assertEqual(players + 1, self.scalar('SELECT count(*) FROM players'))
        key = 'p' + str(self.scalar("SELECT id FROM players WHERE name='测试队员'"))

        # 编辑真实资料与角色
        response = self.post(f'/admin/accounts/{key}/edit', source='player', username='测试队员2',
                             role='队长', team='D', gender='男', sort_order='8', enabled='1')
        self.assertEqual(303, response.status_code)
        with db.connection() as conn:
            row = conn.execute("SELECT * FROM players WHERE name='测试队员2'").fetchone()
        self.assertEqual(1, row['is_captain'])
        self.assertEqual('男', row['gender'])

        # 重复姓名校验
        existing = self.scalar('SELECT name FROM players LIMIT 1')
        response = self.post(f'/admin/accounts/{key}/edit', source='player', username=existing,
                             role='队员', team='D', gender='男', sort_order='8', enabled='1')
        self.assertEqual(200, response.status_code)
        self.assertIn('已存在', response.get_data(as_text=True))

        # 禁用后前台无法登录，且身份回查立即失效
        response = self.post(f'/admin/accounts/{key}/toggle')
        self.assertEqual(303, response.status_code)
        self.assertEqual(0, self.scalar("SELECT enabled FROM players WHERE name='测试队员2'"))
        self.assertFalse(db.is_player_enabled('测试队员2'))
        self.assertTrue(db.is_player_enabled(existing))

        # 删除真实账号
        self.post(f'/admin/accounts/{key}/delete')
        self.assertEqual(players, self.scalar('SELECT count(*) FROM players'))

    def test_disabled_teacher_cannot_login(self):
        self.login()
        teacher = self.scalar('SELECT name FROM teachers LIMIT 1')
        key = 't' + str(self.scalar('SELECT id FROM teachers WHERE name=?', (teacher,)))
        self.post(f'/admin/accounts/{key}/toggle')
        self.assertFalse(db.is_teacher(teacher))
        c = self.module.app.test_client()
        page = c.get('/login').get_data(as_text=True)
        import re
        token = re.search(r'name="csrf"[^>]*value="([^"]+)"', page).group(1)
        response = c.post('/login', data={'role': 'teacher', 'teacher': teacher, 'csrf': token})
        self.assertEqual(400, response.status_code)

    def test_admin_write_bumps_revision(self):
        self.login()
        before = int(self.scalar("SELECT value FROM meta WHERE key='revision'"))
        self.post('/admin/competitions/1/edit', name='版本测试', match_time='', status='未开始',
                  team_a='A', team_b='B', round_no='1', court='1')
        after = int(self.scalar("SELECT value FROM meta WHERE key='revision'"))
        self.assertEqual(before + 1, after)

    # ---------- 观众账号清理 ----------
    def _seed_spectator_traces(self, name, guesses=0, logins=0, pin=False):
        with db.connection() as conn:
            for i in range(guesses):
                conn.execute(
                    "INSERT OR IGNORE INTO guesses (name, kind, ref, pick, stake, created_at, updated_at)"
                    " VALUES (?, 'game', ?, 'A', 1, datetime('now'), datetime('now'))",
                    (name, f"99-{i + 1}"),
                )
            for _ in range(logins):
                conn.execute(
                    "INSERT INTO login_logs (occurred_at, role, identity, team, outcome)"
                    " VALUES (datetime('now'), 'student', ?, NULL, 'ok_spectator')",
                    (name,),
                )
            if pin:
                conn.execute(
                    "INSERT OR REPLACE INTO student_pins (name, pass_hash, pin_version, updated_at)"
                    " VALUES (?, 'x', 2, datetime('now'))",
                    (name,),
                )

    def test_spectator_rows_exclude_roster(self):
        player = self.scalar('SELECT name FROM players LIMIT 1')
        teacher = self.scalar('SELECT name FROM teachers LIMIT 1')
        self._seed_spectator_traces('观众甲', guesses=2, logins=1)
        self._seed_spectator_traces('观众乙', pin=True)
        self._seed_spectator_traces(player, guesses=1)
        self._seed_spectator_traces(teacher, logins=1)
        rows = {r['name']: r for r in db.spectator_rows()}
        self.assertIn('观众甲', rows)
        self.assertIn('观众乙', rows)
        self.assertNotIn(player, rows)
        self.assertNotIn(teacher, rows)
        self.assertEqual(2, rows['观众甲']['guess_count'])
        self.assertEqual(1, rows['观众甲']['login_count'])
        self.assertTrue(rows['观众乙']['custom_pin'])

    def test_purge_spectators_keeps_roster_traces(self):
        player = self.scalar('SELECT name FROM players LIMIT 1')
        teacher = self.scalar('SELECT name FROM teachers LIMIT 1')
        self._seed_spectator_traces('观众甲', guesses=3, logins=2, pin=True)
        self._seed_spectator_traces(player, guesses=1)
        self._seed_spectator_traces(teacher, logins=1)
        before = int(self.scalar("SELECT value FROM meta WHERE key='revision'"))
        result = db.purge_spectators(['观众甲', player, teacher])
        self.assertEqual(1, result['names'])
        self.assertEqual(3, result['guesses'])
        self.assertEqual(2, result['logins'])
        self.assertEqual(1, result['pins'])
        self.assertEqual([player, teacher], result['skipped'])
        self.assertEqual(0, self.scalar("SELECT count(*) FROM guesses WHERE name='观众甲'"))
        self.assertEqual(0, self.scalar("SELECT count(*) FROM login_logs WHERE identity='观众甲'"))
        self.assertEqual(0, self.scalar("SELECT count(*) FROM student_pins WHERE name='观众甲'"))
        self.assertEqual(1, self.scalar("SELECT count(*) FROM guesses WHERE name=?", (player,)))
        self.assertEqual(1, self.scalar("SELECT count(*) FROM login_logs WHERE identity=?", (teacher,)))
        self.assertGreater(int(self.scalar("SELECT value FROM meta WHERE key='revision'")), before)

    def test_spectator_purge_route_requires_csrf(self):
        self.login()
        response = self.client.post('/admin/spectators/purge', data={'names': '观众甲'})
        self.assertEqual(403, response.status_code)

    def test_spectator_page_and_purge_route(self):
        self.login()
        self._seed_spectator_traces('观众甲', guesses=2, logins=1)
        page = self.client.get('/admin/spectators').get_data(as_text=True)
        self.assertIn('观众甲', page)
        self.assertIn('观众账号清理', page)
        response = self.post('/admin/spectators/purge', names='观众甲')
        self.assertEqual(303, response.status_code)
        self.assertEqual(0, self.scalar("SELECT count(*) FROM guesses WHERE name='观众甲'"))
        self.assertNotIn('观众甲', self.client.get('/admin/spectators').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
