"""Flask + SQLite 端到端测试。"""

import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import app as webapp  # noqa: E402  (包 app，入口 app/app.py)
from app.solver import flow_sets, parse_csv  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "sample", "slope_cm.csv")
CORRECTION = os.path.join(os.path.dirname(__file__), "..", "sample", "correction_cm.csv")
ALTERNATIVES = os.path.join(
    os.path.dirname(__file__), "..", "sample", "check_alternatives.csv"
)


def _upload(client, path, error_limit, name="demo", url=None):
    with open(path, "rb") as fh:
        data = {"name": name, "error_limit": str(error_limit),
                "csv_file": (io.BytesIO(fh.read()), os.path.basename(path))}
        return client.post(url or "/projects", data=data, follow_redirects=True)


class WebFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        webapp.DB_PATH = os.path.join(self.tmp.name, "test.db")
        webapp.app.config.update(TESTING=True)
        webapp.init_db()
        self.client = webapp.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_lifecycle(self):
        # 1) 初次上传，9 cm 误差 -> 成功，出现下载链接
        page = _upload(self.client, SAMPLE, 9)
        self.assertEqual(page.status_code, 200)
        self.assertIn("转换成功", page.get_data(as_text=True))
        project_url = page.request.path
        pid = int(project_url.rstrip("/").split("/")[-1])

        # 2) 下载分米 CSV，流向与厘米输入一致
        dl = self.client.get(f"/projects/{pid}/download")
        self.assertEqual(dl.status_code, 200)
        dm_grid = parse_csv(dl.get_data(as_text=True))
        with open(SAMPLE, encoding="utf-8") as fh:
            cm_grid = parse_csv(fh.read())
        self.assertEqual(flow_sets(dm_grid), flow_sets(cm_grid))

        # 3) 锁定沟口 (6,6)=8 dm 后重新转换，锁定被遵守
        resp = self.client.post(
            f"/projects/{pid}/locks",
            data={"row": "6", "col": "6", "dm_value": "8"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            f"/projects/{pid}/convert", data={"error_limit": "9"},
            follow_redirects=True,
        )
        self.assertIn("转换成功", resp.get_data(as_text=True))
        dl = self.client.get(f"/projects/{pid}/download")
        self.assertEqual(parse_csv(dl.get_data(as_text=True))[5][5], 8)

        # 4) 上传同尺寸测绘更正，锁定沿用
        page = _upload(
            self.client, CORRECTION, 9,
            url=f"/projects/{pid}/correction",
        )
        self.assertIn("测绘更正转换成功", page.get_data(as_text=True))
        dl = self.client.get(f"/projects/{pid}/download")
        new_dm = parse_csv(dl.get_data(as_text=True))
        self.assertEqual(new_dm[5][5], 8)
        with open(CORRECTION, encoding="utf-8") as fh:
            new_cm = parse_csv(fh.read())
        self.assertEqual(flow_sets(new_dm), flow_sets(new_cm))

        # 5) 拒绝尺寸不一致的更正
        wrong = io.BytesIO(b"1,2\n3,4\n")
        resp = self.client.post(
            f"/projects/{pid}/correction",
            data={"error_limit": "9", "csv_file": (wrong, "wrong.csv")},
            follow_redirects=True,
        )
        self.assertIn("尺寸", resp.get_data(as_text=True))

        # 6) 快照历史里至少 3 条成功记录
        import sqlite3
        conn = sqlite3.connect(webapp.DB_PATH)
        count = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE project_id=? AND status='ok'", (pid,)
        ).fetchone()[0]
        conn.close()
        self.assertGreaterEqual(count, 3)

    def test_tight_error_keeps_previous_basemap(self):
        # 先用 20 cm 得到一份成功底图
        page = _upload(self.client, SAMPLE, 20)
        pid = int(page.request.path.rstrip("/").split("/")[-1])
        good_dl = self.client.get(f"/projects/{pid}/download").get_data(as_text=True)

        # 再用 0 cm 重转，必然失败
        resp = self.client.post(
            f"/projects/{pid}/convert", data={"error_limit": "0"},
            follow_redirects=True,
        )
        text = resp.get_data(as_text=True)
        self.assertIn("最小可行误差", text)
        self.assertIn("上一份可下载底图", text)

        # 下载内容仍是旧的成功底图
        self.assertEqual(
            self.client.get(f"/projects/{pid}/download").get_data(as_text=True),
            good_dl,
        )

    def test_hard_conflict_message(self):
        page = _upload(self.client, SAMPLE, 9)
        pid = int(page.request.path.rstrip("/").split("/")[-1])
        # 制造高低颠倒的锁定（沟口与左邻的高低关系被反转）
        self.client.post(f"/projects/{pid}/locks",
                         data={"row": "6", "col": "6", "dm_value": "17"})
        self.client.post(f"/projects/{pid}/locks",
                         data={"row": "6", "col": "5", "dm_value": "16"})
        resp = self.client.post(
            f"/projects/{pid}/convert", data={"error_limit": "9"},
            follow_redirects=True,
        )
        self.assertIn("无法同时满足", resp.get_data(as_text=True))
        # 放宽到极大误差依然冲突
        resp = self.client.post(
            f"/projects/{pid}/convert", data={"error_limit": "100000"},
            follow_redirects=True,
        )
        self.assertIn("无法同时满足", resp.get_data(as_text=True))

    def test_bad_upload_rejected(self):
        resp = self.client.post(
            "/projects",
            data={"name": "x", "error_limit": "9",
                  "csv_file": (io.BytesIO(b"1,2\n3\n"), "bad.csv")},
            follow_redirects=True,
        )
        self.assertIn("不一致", resp.get_data(as_text=True))

    def test_correction_failure_preserves_basemap(self):
        page = _upload(self.client, SAMPLE, 20)
        pid = int(page.request.path.rstrip("/").split("/")[-1])
        good_dl = self.client.get(f"/projects/{pid}/download").get_data(as_text=True)
        # 锁一个苛刻值，再用 0 误差上传更正 -> 失败：新增失败记录但底图不变
        self.client.post(f"/projects/{pid}/locks",
                         data={"row": "1", "col": "1", "dm_value": "33"})
        before_total = self._snapshot_count(pid)
        before_ok = self._ok_snapshot_count(pid)
        resp = _upload(self.client, CORRECTION, 0,
                       url=f"/projects/{pid}/correction")
        self.assertIn("已保留", resp.get_data(as_text=True))
        self.assertEqual(self._snapshot_count(pid), before_total + 1)
        self.assertEqual(self._ok_snapshot_count(pid), before_ok)
        self.assertEqual(
            self.client.get(f"/projects/{pid}/download").get_data(as_text=True),
            good_dl,
        )

    def _snapshot_count(self, pid):
        import sqlite3
        conn = sqlite3.connect(webapp.DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM snapshots WHERE project_id=?", (pid,)).fetchone()[0]
        conn.close()
        return n

    def _ok_snapshot_count(self, pid):
        import sqlite3
        conn = sqlite3.connect(webapp.DB_PATH)
        n = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE project_id=? AND status='ok'", (pid,)
        ).fetchone()[0]
        conn.close()
        return n

    # ------------------------------------------------------------------ #
    # 双路来水核查
    # ------------------------------------------------------------------ #
    def _make_project(self):
        page = _upload(self.client, SAMPLE, 9)
        return int(page.request.path.rstrip("/").split("/")[-1])

    def _create_check(self, pid, snapshot_id=1, positions=None, alt_path=ALTERNATIVES):
        positions = positions or {
            "source_a_row": "1", "source_a_col": "1",
            "source_b_row": "1", "source_b_col": "6",
            "outlet_row": "6", "outlet_col": "6",
        }
        with open(alt_path, "rb") as fh:
            data = dict(positions)
            data["snapshot_id"] = str(snapshot_id)
            data["csv_file"] = (io.BytesIO(fh.read()), os.path.basename(alt_path))
            return self.client.post(
                f"/projects/{pid}/checks", data=data, follow_redirects=True
            )

    def _check_id(self, resp):
        return int(resp.request.path.rstrip("/").split("/")[-1])

    def test_check_creates_dual_route(self):
        pid = self._make_project()
        resp = self._create_check(pid)
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True)
        self.assertIn("核查已建立", text)
        self.assertIn("找到一套成立的测段选择", text)
        self.assertIn("来水甲路线", text)
        self.assertIn("来水乙路线", text)
        # 页面上应有两份逐格路线（11 格 + 6 格，含沟口）
        self.assertIn("来水甲路线（11 格）", text)
        self.assertIn("来水乙路线（6 格）", text)

    def test_check_positions_must_be_distinct(self):
        pid = self._make_project()
        resp = self._create_check(
            pid,
            positions={
                "source_a_row": "6", "source_a_col": "6",
                "source_b_row": "6", "source_b_col": "6",
                "outlet_row": "6", "outlet_col": "6",
            },
        )
        self.assertIn("位置不能重合", resp.get_data(as_text=True))

    def test_check_route_does_not_exist_reports_message(self):
        pid = self._make_project()
        # 沟口设在左上角，两来水在其下游 -> 无法逆流到达。
        alt = io.BytesIO(
            "来水甲,6,1,231\n来水乙,6,6,75\n沟口,1,1,317\n".encode()
        )
        data = {
            "snapshot_id": "1",
            "source_a_row": "6", "source_a_col": "1",
            "source_b_row": "6", "source_b_col": "6",
            "outlet_row": "1", "outlet_col": "1",
            "csv_file": (alt, "bad.csv"),
        }
        resp = self.client.post(
            f"/projects/{pid}/checks", data=data, follow_redirects=True
        )
        self.assertIn("找不到双路", resp.get_data(as_text=True))

    def test_check_accepts_key_cells_outside_segments(self):
        pid = self._make_project()
        # 只列中间格子；三个关键格不在测段里，仍按快照厘米值参与核查。
        resp = self._create_check(pid, alt_path=os.path.join(
            os.path.dirname(__file__), "..", "sample", "check_alternatives.csv"
        ))
        self.assertIn("核查已建立", resp.get_data(as_text=True))

        alt = io.BytesIO(b"only,1,2,299\n")
        data = {
            "snapshot_id": "1", "source_a_row": "1", "source_a_col": "1",
            "source_b_row": "1", "source_b_col": "6",
            "outlet_row": "6", "outlet_col": "6",
            "csv_file": (alt, "partial.csv"),
        }
        resp = self.client.post(f"/projects/{pid}/checks", data=data,
                                follow_redirects=True)
        text = resp.get_data(as_text=True)
        self.assertIn("核查已建立", text)
        self.assertIn("找到一套成立的测段选择", text)

    def test_check_requires_ok_snapshot(self):
        pid = self._make_project()
        # 制造一次失败快照，其 id 不能用于核查
        self.client.post(f"/projects/{pid}/convert", data={"error_limit": "0"},
                         follow_redirects=True)
        import sqlite3
        conn = sqlite3.connect(webapp.DB_PATH)
        bad_id = conn.execute(
            "SELECT id FROM snapshots WHERE project_id=? AND status!='ok' "
            "ORDER BY id DESC LIMIT 1", (pid,)
        ).fetchone()[0]
        conn.close()
        resp = self._create_check(pid, snapshot_id=bad_id)
        self.assertIn("不是转换成功的快照", resp.get_data(as_text=True))

    def test_check_rejects_duplicate_cell_and_too_many_segments(self):
        pid = self._make_project()
        dup = io.BytesIO(b"A,1,1,317\nB,1,1,300\n")
        data = {
            "snapshot_id": "1", "source_a_row": "1", "source_a_col": "1",
            "source_b_row": "1", "source_b_col": "6",
            "outlet_row": "6", "outlet_col": "6",
            "csv_file": (dup, "dup.csv"),
        }
        resp = self.client.post(f"/projects/{pid}/checks", data=data, follow_redirects=True)
        self.assertIn("每格只能填写一次", resp.get_data(as_text=True))

        rows = "\n".join(f"S{i},1,{i + 1},10" for i in range(9))
        many = io.BytesIO(rows.encode())
        data["csv_file"] = (many, "many.csv")
        resp = self.client.post(f"/projects/{pid}/checks", data=data, follow_redirects=True)
        self.assertIn("最多支持 8 个测段", resp.get_data(as_text=True))

    def test_confirm_and_revoke_use_precomputed_flows(self):
        pid = self._make_project()
        resp = self._create_check(pid)
        cid = self._check_id(resp)
        text = resp.get_data(as_text=True)
        self.assertIn("未确认", text)
        # 默认方案：甲路经过 (3,3)
        self.assertIn("(3,3)", text)

        # 确认「甲腰」用备选值（备选值 300 会让甲路改道到 (4,2)→(4,3)）
        resp = self.client.post(
            f"/projects/{pid}/checks/{cid}/confirm",
            data={"segment": "甲腰", "use_alt": "1"},
            follow_redirects=True,
        )
        text = resp.get_data(as_text=True)
        self.assertIn("已确认", text)
        self.assertIn("已确认用备选值", text)
        self.assertIn("甲腰·备", text)
        self.assertIn("(4,2)", text)

        # 撤销后恢复建议取值与原路线
        resp = self.client.post(
            f"/projects/{pid}/checks/{cid}/revoke",
            data={"segment": "甲腰"},
            follow_redirects=True,
        )
        text = resp.get_data(as_text=True)
        self.assertIn("已撤销", text)
        self.assertIn("甲腰·快", text)
        self.assertNotIn("(4,2)", text)

    def test_confirm_survives_restart(self):
        pid = self._make_project()
        cid = self._check_id(self._create_check(pid))
        self.client.post(
            f"/projects/{pid}/checks/{cid}/confirm",
            data={"segment": "乙尾", "use_alt": "0"},
            follow_redirects=True,
        )
        # 模拟重启：扔掉当前连接，新建 client 读同一个 DB
        import sqlite3
        conn = sqlite3.connect(webapp.DB_PATH)
        row = conn.execute(
            "SELECT use_alt FROM check_confirmations WHERE check_id=? AND segment=?",
            (cid, "乙尾"),
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 0)
        fresh = webapp.app.test_client()
        page = fresh.get(f"/projects/{pid}/checks/{cid}").get_data(as_text=True)
        self.assertIn("已确认用快照值", page)

    def test_correction_does_not_change_check_snapshot(self):
        pid = self._make_project()
        cid = self._check_id(self._create_check(pid))
        before = self.client.get(f"/projects/{pid}/checks/{cid}").get_data(as_text=True)
        # 上传后续测绘更正
        with open(CORRECTION, "rb") as fh:
            correction_bytes = fh.read()
        self.client.post(
            f"/projects/{pid}/correction",
            data={"error_limit": "9",
                  "csv_file": (io.BytesIO(correction_bytes), "correction.csv")},
            follow_redirects=True,
        )
        after = self.client.get(f"/projects/{pid}/checks/{cid}").get_data(as_text=True)
        import sqlite3
        conn = sqlite3.connect(webapp.DB_PATH)
        snap_id, frozen_cm, frozen_pre = conn.execute(
            "SELECT snapshot_id, input_cm, precomputed FROM checks WHERE id=?", (cid,)
        ).fetchone()
        conn.close()
        self.assertEqual(snap_id, 1)
        # 核查冻结的厘米底图是快照 #1 的原始值（(1,5)=231），不是更正后的 225。
        frozen = json.loads(frozen_cm)
        self.assertEqual(frozen[0][4], 231)
        self.assertEqual(json.loads(frozen_pre)["masks"], json.loads(frozen_pre)["masks"])
        # 核查页的路线与结果不受更正影响（更正改了 (1,5)=231->225 等格子）
        self.assertIn("来水甲路线（11 格）", after)
        self.assertIn("#1", after)
        self.assertEqual(
            before.split("<section class=\"card\">")[2],
            after.split("<section class=\"card\">")[2],
        )

    def test_confirmation_save_failure_keeps_previous(self):
        pid = self._make_project()
        cid = self._check_id(self._create_check(pid))
        self.client.post(
            f"/projects/{pid}/checks/{cid}/confirm",
            data={"segment": "乙尾", "use_alt": "1"},
            follow_redirects=True,
        )
        original = webapp._save_confirmation

        def boom(*_args, **_kwargs):
            import sqlite3
            raise sqlite3.OperationalError("simulated disk failure")

        webapp._save_confirmation = boom
        try:
            resp = self.client.post(
                f"/projects/{pid}/checks/{cid}/confirm",
                data={"segment": "乙尾", "use_alt": "0"},
                follow_redirects=True,
            )
        finally:
            webapp._save_confirmation = original
        text = resp.get_data(as_text=True)
        self.assertIn("确认保存失败", text)
        # 此前的确认（备选值）仍在
        self.assertIn("已确认用备选值", text)
        # 此前的核查结果（乙尾按备选值 152）也仍在，而非失败请求里的快照值 143
        self.assertIn("乙尾·备", text)
        self.assertIn("152 cm", text)

    def test_check_template_download(self):
        pid = self._make_project()
        resp = self.client.get(f"/projects/{pid}/checks/template")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("测段,行号,列号,备选厘米值", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
