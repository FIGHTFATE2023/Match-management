"""竞猜回归测试：只操作临时数据库。运行 python -m unittest discover -s tests -v。"""
from contextlib import closing
import importlib.util
import sqlite3
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

class GuessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db = db.DB_PATH
        # 使用应用原始源码与真实路由，测试密钥不读写运行包的密钥文件。
        spec = importlib.util.spec_from_file_location('guess_test_app', BACKEND / 'app.py')
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        source = (BACKEND / 'app.py').read_text(encoding='utf-8')
        with patch.object(db, 'init_db'), patch.object(admin_module, 'init_admin_db'):
            exec(compile(source.replace('app.secret_key = load_secret()', "app.secret_key = 'isolated-test-secret'"), str(BACKEND / 'app.py'), 'exec'), cls.module.__dict__)
        cls.module.app.testing = True

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(setattr, db, 'DB_PATH', db.DB_PATH)
        self.addCleanup(setattr, db, 'LOCAL_DIR', db.LOCAL_DIR)
        db.LOCAL_DIR = Path(self.temp.name)
        db.DB_PATH = db.LOCAL_DIR / 'match.db'
        db.init_db()
        self.client = self.module.app.test_client()
        with self.client.session_transaction() as session:
            session.update(role='student', name='测试观众', pinv=1, form_csrf='test-csrf')
        self.headers = {'X-Requested-With': 'XMLHttpRequest'}

    def post(self, url, **data):
        return self.client.post(url, data=dict(csrf='test-csrf', **data), headers=self.headers)

    def publish(self):
        with db.connection() as conn:
            conn.executemany('INSERT OR REPLACE INTO lineup_publications VALUES (?, ?, ?)', [(team, db._now(), 'test') for team in db.ROSTER])
        self.assertTrue(db.all_published())

    def points(self, ref, team, count):
        for _ in range(count):
            self.assertTrue(db.add_point(ref, team, 'A', 'B')['ok'])

    def first_game(self):
        self.assertTrue(db.start_score_game('1-1', 1, 1, '1', '裁判')[0])
        self.points('1-1', 'B', 5)
        self.points('1-1', 'A', 15)
        self.assertTrue(db.finish_score_game('1-1')['ok'])

    def test_routes_publication_validation_and_csrf(self):
        self.assertEqual(self.client.get('/guess').status_code, 200)
        self.assertEqual(self.post('/guess/game', ref='1-1', pick='A').status_code, 403)
        self.publish()
        for ref, pick in [('bad','A'), ('1-7','A'), ('99-1','A'), ('1-1','C')]:
            self.assertEqual(self.post('/guess/game', ref=ref, pick=pick).status_code, 400)
        self.assertEqual(self.client.post('/guess/game', data={'ref':'1-1', 'pick':'A'}).status_code, 403)
        self.assertEqual(self.post('/guess/game', ref='01-01', pick='A').status_code, 200)
        self.assertEqual(db.get_user_guesses('测试观众')['games'], {'1-1':'A'})
        self.assertEqual(self.post('/guess/game/cancel', ref='1-1').status_code, 200)
        self.assertEqual(db.get_user_guesses('测试观众')['games'], {})
        self.assertEqual(self.client.get('/guess').status_code, 200)
        self.assertEqual(self.client.get('/standings').status_code, 200)
        self.assertEqual(self.client.get('/api/guess').status_code, 200)

    def test_first_fifteen_uses_increment_and_order_and_confirmation(self):
        db.save_guess('测试观众', 'game', '1-2', 'B', 1)
        db.save_guess('D老师', 'game', '1-2', 'B', 2)
        db.save_guess('对方观众', 'game', '1-2', 'A', 1)
        self.first_game()  # 继承 15:5
        self.assertTrue(db.start_score_game('1-2', 1, 2, '1', '裁判')[0])
        self.assertIsNone(db.guess_results()['1-2']['winner'])
        self.points('1-2', 'B', 15)  # 累计 15:20，本局 B 已先得十五分
        self.assertEqual(db.guess_results()['1-2']['winner'], 'B')
        self.assertIsNone(db.guess_results('测试观众')['1-2']['settled'])
        self.assertTrue(all(row['score']==0 for row in db.standings()['guesses']))
        self.points('1-2', 'A', 15)  # 累计 30:20，A 达本局结束目标，竞猜仍是 B
        self.assertTrue(db.finish_score_game('1-2')['ok'])
        outcome = db.guess_results('测试观众')['1-2']
        self.assertEqual(outcome['winner'], 'B')
        self.assertEqual(outcome['settled'], {'win':True, 'gain':1.5})
        scores = {row['name']:row for row in db.standings()['guesses']}
        self.assertEqual(scores['D老师']['score'], 2.5)
        self.assertEqual(scores['测试观众']['score'], 1.5)
        self.assertEqual(scores['对方观众']['score'], 0)
        self.assertEqual(db.score_game('1-2')['score_a'], 30)
        self.assertEqual(db.game_rule(6)['target'], 90)
        self.assertIsNone(db.score_game('1-2')['winner'])

    def test_undo_removes_provisional_winner(self):
        self.first_game()
        db.start_score_game('1-2', 1, 2, '1', '裁判')
        self.points('1-2', 'B', 15)
        self.assertEqual(db.guess_results()['1-2']['winner'], 'B')
        self.assertTrue(db.undo_point('1-2')['ok'])
        self.assertIsNone(db.guess_results()['1-2']['winner'])
        self.points('1-2', 'A', 15)
        self.assertEqual(db.guess_results()['1-2']['winner'], 'A')

    def test_start_locks_champion_and_game_including_noncanonical_ref(self):
        self.publish()
        self.assertEqual(self.post('/guess/champion', pick='A').status_code, 200)
        self.assertEqual(self.post('/guess/game', ref='1-1', pick='A').status_code, 200)
        db.start_score_game('1-1', 1, 1, '1', '裁判')
        for url, data in [('/guess/champion', {'pick':'B'}), ('/guess/game', {'ref':'01-01','pick':'B'}), ('/guess/game/cancel', {'ref':'1-1'})]:
            self.assertEqual(self.post(url, **data).status_code, 403)
        self.assertFalse(db.save_guess('测试观众', 'game','1-1','B'))
        self.assertFalse(db.delete_guess('测试观众','game','1-1'))
        self.assertEqual(db.get_user_guesses('测试观众')['games']['1-1'], 'A')
        self.assertEqual(self.post('/guess/game', ref='1-2', pick='B').status_code, 200)

    def test_teacher_stake_and_unsettled_leaderboard(self):
        self.publish()
        with self.client.session_transaction() as session:
            session.update(role='teacher', name='D老师')
        self.assertEqual(self.post('/guess/game', ref='1-1', pick='A').status_code, 200)
        with db.connection() as conn:
            self.assertEqual(conn.execute('SELECT stake FROM guesses').fetchone()['stake'], 2)
        data = self.client.get('/api/guess').get_json()
        self.assertEqual(data['standings'][0]['score_text'], '0.00')
        self.assertEqual(self.client.get('/guess').status_code, 200)

    def test_old_guess_schema_migrates_idempotently(self):
        with db.connection() as conn:
            conn.execute('DROP TABLE guesses')
            conn.execute('CREATE TABLE guesses (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, ref TEXT, pick TEXT, created_at TEXT, updated_at TEXT, UNIQUE(name, kind, ref))')
            conn.execute("INSERT INTO guesses VALUES (1, 'D老师', 'game', '1-1', 'A', '', '')")
        db.init_db()
        db.init_db()
        with db.connection() as conn:
            row=conn.execute('SELECT * FROM guesses').fetchone()
            self.assertEqual((row['pick'],row['stake']), ('A',2))

    def test_existing_database_copy_keeps_match_data(self):
        if not self.original_db.exists():
            self.skipTest('no existing database')
        # 在线数据库通过 SQLite backup 读取到临时库；绝不写入在线数据库。
        with closing(sqlite3.connect(f'file:{self.original_db.as_posix()}?mode=ro',uri=True)) as src:
            with closing(sqlite3.connect(db.DB_PATH)) as dest:
                src.backup(dest)
        tables=['players','score_games','score_events','matches','team_lineups','match_lineups','custom_matches']
        with db.connection() as conn:
            before={table:[tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in tables}
        db.init_db()
        with db.connection() as conn:
            after={table:[tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in tables}
        self.assertEqual(before,after)
        self.assertEqual(self.client.get('/standings').status_code,200)

    def test_champion_settles_only_after_four_captains_confirm(self):
        db.save_guess('冠军测试', 'champion', 'champion', 'A')
        self.assertIsNone(db.standings()['champion'])
        matches = db.all_matches()
        for match in matches:
            for game in db.GAMES:
                ref = db.match_ref(match['id'], game['no'])
                self.assertTrue(db.start_score_game(ref, match['id'], game['no'], '1', '裁判')[0])
                for _ in range(15):
                    self.assertTrue(db.add_point(ref, match['team_a'], match['team_a'], match['team_b'])['ok'])
                self.assertTrue(db.finish_score_game(ref)['ok'])
            if match != matches[-1]:
                self.assertIsNone(db.standings()['champion'])
        board = db.standings()
        self.assertIsNone(board['champion'])
        self.assertEqual(board['guesses'][0]['score'], 0)
        for captain, team in db.CAPTAINS.items():
            result = db.confirm_tournament_end(team, captain)
            self.assertTrue(result['ok'])
        board = db.standings()
        self.assertEqual(board['champion'], 'A')
        self.assertEqual(board['champions'], ['A'])
        self.assertEqual(set(board['champion_rosters']['A']), set(db.ROSTER['A']))
        self.assertEqual(board['finished_games'], 36)
        self.assertEqual(board['finished_matches'], 6)
        self.assertEqual(board['guesses'][0]['score'], 5)
        self.assertEqual(board['teams'][0]['wins'], 3)

    def test_live_prediction_ratios_and_personal_scores(self):
        self.publish()
        db.save_guess('测试观众', 'game', '1-1', 'A', 1)
        db.save_guess('D老师', 'game', '1-1', 'A', 2)
        db.save_guess('另一观众', 'game', '1-1', 'B', 1)
        db.start_score_game('1-1', 1, 1, '1', '裁判')
        item = self.client.get('/api/live').get_json()['items']['1-1']
        self.assertEqual(item['predict'], {'a_count': 2, 'b_count': 1, 'a_stake': 3, 'b_stake': 1})
        self.assertEqual((item['my_pick'], item['expected'], item['settled']), ('A', 1.5, None))
        with self.client.session_transaction() as session:
            session.update(role='teacher', name='D老师')
        teacher = self.client.get('/api/live').get_json()['items']['1-1']
        self.assertEqual(teacher['expected'], 2.5)
        with self.client.session_transaction() as session:
            session.update(role='student', name='未竞猜观众', pinv=1)
        observer = self.client.get('/api/live').get_json()['items']['1-1']
        self.assertEqual(observer['predict'], item['predict'])
        self.assertIsNone(observer['expected'])
        self.assertIsNone(observer['my_pick'])
        self.assertIsNone(observer['settled'])
        html = self.client.get('/schedule').get_data(as_text=True)
        self.assertIn('data-role="predict-bar"', html)
        self.assertIn('data-role="predict-hint"', html)
        self.points('1-1', 'A', 15)
        db.finish_score_game('1-1')
        winner = db.live_payload('测试观众')['1-1']
        loser = db.live_payload('另一观众')['1-1']
        self.assertEqual(winner['settled'], {'win': True, 'gain': 1.5})
        self.assertEqual(loser['settled'], {'win': False, 'gain': 0.0})

    def test_live_predictions_without_opposing_guesses(self):
        db.save_guess('测试观众', 'game', '1-2', 'B', 1)
        self.first_game()
        self.assertIsNone(db.live_payload('测试观众')['1-1']['predict'])
        db.start_score_game('1-2', 1, 2, '1', '裁判')
        item = db.live_payload('测试观众')['1-2']
        self.assertEqual(item['predict']['a_stake'], 0)
        self.assertEqual(item['predict']['b_stake'], 1)
        self.assertEqual(item['expected'], 1.0)
        self.points('1-2', 'B', 15)
        self.points('1-2', 'A', 15)
        db.finish_score_game('1-2')
        item = db.live_payload('测试观众')['1-2']
        self.assertEqual((item['score_a'], item['score_b']), (30, 20))
        self.assertEqual(item['settled'], {'win': True, 'gain': 1.0})

    def confirm_all(self):
        for captain, team in db.CAPTAINS.items():
            self.assertTrue(db.confirm_tournament_end(team, captain)['ok'])

    def captain_session(self):
        captain = next(iter(db.CAPTAINS))
        with self.client.session_transaction() as session:
            session.update(role='student', name=captain, pinv=1, form_csrf='test-csrf')
        return captain

    def test_end_confirm_permissions_and_duplicate_confirmation(self):
        self.assertEqual(self.post('/tournament/confirm').status_code, 403)
        captain = self.captain_session()
        self.assertEqual(self.client.post('/tournament/confirm').status_code, 403)
        # 用户提交的组别不能覆盖当前登录队长的所属组。
        self.assertEqual(self.post('/tournament/confirm', team='B').status_code, 200)
        self.assertEqual(self.post('/tournament/confirm').status_code, 200)
        status = self.client.get('/api/tournament').get_json()
        self.assertEqual(status['confirmed_teams'], ['A'])
        self.assertEqual(status['confirmed_count'], 1)
        self.assertFalse(status['finalized'])
        self.assertFalse(db.confirm_tournament_end('B', captain)['ok'])
        self.assertIn('比赛结束确认', self.client.get('/standings').get_data(as_text=True))

    def test_score_and_roster_changes_invalidate_old_confirmations(self):
        captain = next(iter(db.CAPTAINS))
        db.confirm_tournament_end('A', captain)
        self.assertEqual(db.tournament_status()['confirmed_count'], 1)
        db.start_score_game('1-1', 1, 1, '1', '裁判')
        self.assertEqual(db.tournament_status()['confirmed_count'], 0)
        self.assertFalse(db.confirm_tournament_end('A', captain)['ok'])
        self.points('1-1', 'A', 15)
        db.finish_score_game('1-1')
        db.confirm_tournament_end('A', captain)
        self.assertEqual(db.tournament_status()['confirmed_count'], 1)
        with db.connection() as conn:
            conn.execute("UPDATE players SET sort_order = sort_order + 1 WHERE team = 'A' AND is_captain = 0")
        self.assertEqual(db.tournament_status()['confirmed_count'], 0)

    def test_final_snapshot_ties_idempotency_and_write_lock(self):
        db.save_guess('冠军测试', 'champion', 'champion', 'A')
        self.confirm_all()  # 同胜场同净胜分，按现有规则并列冠军。
        status = db.tournament_status()
        self.assertTrue(status['finalized'])
        self.assertEqual(status['champions'], ['A', 'B', 'C', 'D'])
        board = db.standings()
        self.assertEqual(board['guesses'][0]['score'], 5)
        self.assertEqual(board['guesses'][0]['hits'], 1)
        self.confirm_all()
        self.assertEqual(db.standings(), board)
        db.init_db()  # 重启/迁移不重复结算，不清理最终结果。
        self.assertEqual(db.standings(), board)
        self.assertFalse(db.save_guess('另一观众', 'champion', 'champion', 'B'))
        self.captain_session()
        self.assertEqual(self.post('/tournament/confirm').status_code, 200)
        for url in ['/referee/start', '/referee/score', '/referee/custom/create', '/guess/champion', '/team/delete']:
            self.assertEqual(self.post(url).status_code, 409)
        self.assertEqual(self.client.get('/standings').status_code, 200)
        self.assertEqual(self.client.get('/api/guess').get_json()['standings'][0]['score'], 5)
        self.assertEqual(self.client.get('/api/tournament').get_json()['champion_rosters']['D'], db.ROSTER['D'])

    def test_concurrent_final_confirmations_settle_once(self):
        from concurrent.futures import ThreadPoolExecutor
        db.save_guess('并发竞猜', 'champion', 'champion', 'B')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda item: db.confirm_tournament_end(item[1], item[0]), db.CAPTAINS.items()))
        self.assertTrue(all(row['ok'] for row in results))
        self.assertEqual(db.tournament_status()['confirmed_count'], 4)
        self.assertEqual(db.standings()['guesses'][0]['score'], 5)
        with db.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tournament_result').fetchone()[0], 1)

    def test_champion_dialog_on_every_authenticated_page(self):
        self.publish()
        self.confirm_all()
        self.captain_session()
        for url in ['/', '/schedule', '/guess', '/standings', '/referee', '/team']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            html = response.get_data(as_text=True)
            self.assertEqual(html.count('id="champion-dialog"'), 1, url)
            self.assertIn('/static/tournament.js', html, url)
        with self.client.session_transaction() as session:
            session.update(role='teacher', name='D老师')
        response = self.client.get('/referee')
        self.assertEqual(response.status_code, 403)
        self.assertIn('id="champion-dialog"', response.get_data(as_text=True))
        with self.client.session_transaction() as session:
            session.update(role='student', name='观众测试', pinv=1)
        self.assertIn('id="champion-dialog"', self.client.get('/').get_data(as_text=True))
        self.assertNotIn('id="champion-dialog"', self.module.app.test_client().get('/login').get_data(as_text=True))

    def test_champion_dismissal_is_persistent_and_per_user(self):
        self.confirm_all()
        status = self.client.get('/api/tournament').get_json()
        self.assertFalse(status['celebration_dismissed'])
        stamp = status['finalized_at']
        self.assertEqual(self.client.post('/tournament/dismiss', data={'finalized_at': stamp}).status_code, 403)
        self.assertEqual(self.post('/tournament/dismiss', finalized_at='wrong-result').status_code, 409)
        self.assertEqual(self.post('/tournament/dismiss', finalized_at=stamp, name='别的用户').status_code, 200)
        self.assertEqual(self.post('/tournament/dismiss', finalized_at=stamp).status_code, 200)
        self.assertTrue(self.client.get('/api/tournament').get_json()['celebration_dismissed'])
        db.init_db()
        self.assertTrue(db.tournament_status('测试观众')['celebration_dismissed'])
        self.assertFalse(db.tournament_status('别的用户')['celebration_dismissed'])
        with self.client.session_transaction() as session:
            session.update(name='别的用户', pinv=1)
        self.assertFalse(self.client.get('/api/tournament').get_json()['celebration_dismissed'])
        with db.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tournament_champion_dismissals').fetchone()[0], 1)

    def test_single_game_peak_counts_and_legacy_final_snapshot(self):
        import json
        db.save_guess('单次较高', 'game', '1-1', 'A', 2)
        db.save_guess('两次猜中', 'game', '1-1', 'A', 1)
        db.save_guess('两次猜中', 'game', '1-2', 'A', 1)
        db.save_guess('单次较高', 'champion', 'champion', 'A')
        self.first_game()
        db.start_score_game('1-2', 1, 2, '1', '裁判')
        self.points('1-2', 'A', 15)
        db.finish_score_game('1-2')
        rows = db.standings()['guesses']
        self.assertEqual([row['name'] for row in rows], ['单次较高', '两次猜中'])
        self.assertEqual([row['score'] for row in rows], [2, 2])
        self.assertEqual([row['max_gain'] for row in rows], [2, 1])
        self.assertEqual([row['game_bets'] for row in rows], [1, 2])
        # 待结算小局计入竞猜局数，不增加最高单次得分。
        db.save_guess('单次较高', 'game', '1-3', 'B')
        self.assertEqual(db.standings()['guesses'][0]['game_bets'], 2)
        self.assertEqual(db.standings()['guesses'][0]['max_gain_text'], '2.00')
        self.confirm_all()
        board = db.standings()
        peak = next(row for row in board['guesses'] if row['name'] == '单次较高')
        self.assertEqual((peak['score'], peak['hits'], peak['max_gain'], peak['game_bets']), (7, 2, 2, 2))
        # 模拟升级前的最终快照，仅回填新统计，保留原先已结算的冠军分。
        old_board = json.loads(json.dumps(board))
        for row in old_board['guesses']:
            for field in ['max_gain', 'max_gain_text', 'game_bets']:
                del row[field]
        with db.connection() as conn:
            conn.execute('UPDATE tournament_result SET snapshot = ?', (json.dumps(old_board),))
        self.assertEqual(db.standings(), board)
        html = self.client.get('/standings').get_data(as_text=True)
        self.assertIn('最高单次得分', html)
        self.assertIn('竞猜局数', html)
        data = self.client.get('/api/guess').get_json()['standings']
        self.assertEqual(data[0]['max_gain_text'], '2.00')

    def test_all_four_ranking_priorities_then_name_in_final_board(self):
        import json
        fixtures = [
            ('B-name', 9, 4, 4, 5), ('A-name', 9, 4, 4, 5),
            ('Z-fewer-games', 9, 4, 4, 4), ('Z-fewer-hits', 9, 4, 3, 3),
            ('Z-higher-peak', 9, 5, 3, 3), ('Z-higher-total', 10, 3, 4, 4),
        ]
        board = db.standings()
        board['guesses'] = [dict(name=name, score=score, max_gain=peak, max_gain_text=f'{peak:.2f}',
                                hits=hits, bets=games, game_bets=games) for name, score, peak, hits, games in fixtures]
        with db.connection() as conn:
            conn.execute('INSERT INTO tournament_result VALUES (1, ?, ?)', ('ranking-test', json.dumps(board)))
        self.assertEqual([row['name'] for row in db.standings()['guesses']],
                         ['Z-higher-total', 'Z-higher-peak', 'Z-fewer-hits', 'Z-fewer-games', 'A-name', 'B-name'])

    def test_guess_count_includes_champion_up_to_37(self):
        for match in db.all_matches():
            for game in db.GAMES:
                db.save_guess('全部竞猜', 'game', db.match_ref(match['id'], game['no']), match['team_a'])
        db.save_guess('全部竞猜', 'champion', 'champion', 'A')
        db.save_guess('A-one-game', 'game', '1-1', 'A')
        db.save_guess('Z-only-champion', 'champion', 'champion', 'A')
        rows = db.standings()['guesses']
        self.assertEqual([row['name'] for row in rows[:2]], ['A-one-game', 'Z-only-champion'])
        full = next(row for row in rows if row['name'] == '全部竞猜')
        self.assertEqual(full['bets'], 37)
        self.assertEqual(full['game_bets'], 36)
        self.assertEqual(full['max_gain'], 0)
        html = self.client.get('/standings').get_data(as_text=True)
        self.assertIn('37 次', html)
        self.assertIn('<td>37</td>', html)

    def test_guests_must_log_in(self):
        client=self.module.app.test_client()
        self.assertEqual(client.get('/api/guess').status_code,303)
        self.assertEqual(client.get('/guess').status_code,303)

if __name__ == '__main__':
    unittest.main()
