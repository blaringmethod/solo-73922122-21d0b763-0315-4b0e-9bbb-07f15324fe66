"""双路来水核查的单元测试：python -m unittest discover -s tests -v"""

import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import dual  # noqa: E402
from app.solver import ValidationError, flow_sets, parse_csv  # noqa: E402


def _sample_grid():
    with open(os.path.join(ROOT, "sample", "slope_cm.csv"), encoding="utf-8") as fh:
        return parse_csv(fh.read())


def _sample_segments():
    # 来水甲走左上->中列->沟口，来水乙沿右列下行，两路只在 (5,5) 相交。
    return {
        "甲首": {(0, 0): 317, (0, 1): 299},
        "甲中": {(1, 1): 280, (2, 1): 275},
        "甲腰": {(2, 2): 251, (2, 3): 190},
        "甲尾": {(3, 3): 169, (4, 3): 145, (4, 4): 124, (5, 4): 100},
        "乙首": {(0, 5): 223, (1, 5): 207, (2, 5): 165},
        "乙尾": {(3, 5): 143, (4, 5): 103},
        "沟口": {(5, 5): 75},
    }


class TestParseAlternatives(unittest.TestCase):
    def test_header_and_basic(self):
        segs = dual.parse_alternatives(
            "测段,行号,列号,备选厘米值\nA,1,1,100\nA,1,2,90\nB,2,2,80\n"
        )
        self.assertEqual(segs, {"A": {(0, 0): 100, (0, 1): 90}, "B": {(1, 1): 80}})

    def test_no_header(self):
        segs = dual.parse_alternatives("A,1,1,100\n")
        self.assertEqual(segs, {"A": {(0, 0): 100}})

    def test_duplicate_cell_rejected_even_across_segments(self):
        with self.assertRaises(ValidationError):
            dual.parse_alternatives("A,1,1,100\nB,1,1,90\n")

    def test_wrong_columns_rejected(self):
        with self.assertRaises(ValidationError):
            dual.parse_alternatives("A,1,1\n")

    def test_non_integer_rejected(self):
        with self.assertRaises(ValidationError):
            dual.parse_alternatives("A,1,1,ten\n")

    def test_row_col_must_start_at_one(self):
        with self.assertRaises(ValidationError):
            dual.parse_alternatives("A,0,1,10\n")

    def test_more_than_eight_segments_rejected(self):
        lines = [f"S{i},1,{i + 1},10" for i in range(9)]
        with self.assertRaises(ValidationError):
            dual.parse_alternatives("\n".join(lines))

    def test_empty_rejected(self):
        with self.assertRaises(ValidationError):
            dual.parse_alternatives("\n\n")

    def test_roundtrip_export(self):
        segs = {"A": {(0, 0): 100, (0, 1): 90}}
        text = dual.to_alternative_csv(segs)
        self.assertEqual(dual.parse_alternatives(text), segs)


class TestDisjointPaths(unittest.TestCase):
    def test_paths_only_meet_at_outlet(self):
        grid = _sample_grid()
        flows = flow_sets(grid)
        pair = dual.two_disjoint_paths(flows, (0, 0), (0, 5), (5, 5))
        self.assertIsNotNone(pair)
        path_a, path_b = pair
        self.assertEqual(path_a[0], (0, 0))
        self.assertEqual(path_b[0], (0, 5))
        self.assertEqual(path_a[-1], (5, 5))
        self.assertEqual(path_b[-1], (5, 5))
        self.assertEqual(set(path_a) & set(path_b), {(5, 5)})

    def test_every_step_follows_flow(self):
        grid = _sample_grid()
        flows = flow_sets(grid)
        path_a, path_b = dual.two_disjoint_paths(flows, (0, 0), (0, 5), (5, 5))
        for path in (path_a, path_b):
            for src, dst in zip(path, path[1:]):
                self.assertIn(dst, flows[src[0]][src[1]])

    def test_no_dual_route_when_sources_share_first_cell(self):
        # 两个来水格都只有唯一流向、且第一跳到同一格：该格容量为 1，
        # 两条路线无法互不相交。
        grid = [[100, 50], [90, 10]]
        flows = flow_sets(grid)
        # (0,0)->(0,1)->(1,1)；(1,0)->(1,1)；两路只在沟口 (1,1) 相交 -> 可行。
        self.assertIsNotNone(dual.two_disjoint_paths(flows, (0, 0), (1, 0), (1, 1)))
        # 换成两路第一跳都进 (1,0) 的布局：(0,0) 只流向 (1,0)，无法再分岔。
        grid2 = [[100, 10], [90, 80]]
        flows2 = flow_sets(grid2)
        self.assertIsNone(dual.two_disjoint_paths(flows2, (0, 0), (0, 1), (1, 0)))

    def test_random_grids_never_return_crossing_paths(self):
        rng = random.Random(20261123)
        for _ in range(60):
            rows, cols = rng.randint(2, 6), rng.randint(2, 6)
            grid = [[rng.randint(0, 30) for _ in range(cols)] for _ in range(rows)]
            flows = flow_sets(grid)
            cells = [(r, c) for r in range(rows) for c in range(cols)]
            outlet = rng.choice(cells)
            others = [cell for cell in cells if cell != outlet]
            if len(others) < 2:
                continue
            a, b = rng.sample(others, 2)
            pair = dual.two_disjoint_paths(flows, a, b, outlet)
            if pair is None:
                continue
            path_a, path_b = pair
            self.assertEqual(set(path_a) & set(path_b), {outlet})
            for path, start in ((path_a, a), (path_b, b)):
                self.assertEqual(path[0], start)
                self.assertEqual(path[-1], outlet)
                for src, dst in zip(path, path[1:]):
                    self.assertIn(dst, flows[src[0]][src[1]])


class TestPrecomputeAndSelect(unittest.TestCase):
    def setUp(self):
        self.grid = _sample_grid()
        self.segments = _sample_segments()

    def test_precompute_finds_suggestion(self):
        data = dual.precompute(self.grid, (5, 5), (0, 0), (0, 5), self.segments)
        self.assertIsNotNone(data["suggested"])
        entry = data["masks"][str(data["suggested"])]
        self.assertTrue(entry["ok"])
        self.assertEqual(sorted(data["masks"]), sorted(str(i) for i in range(128)))
        self.assertEqual(len(data["segment_names"]), 7)

    def test_precompute_paths_verify_against_stored_flows(self):
        data = dual.precompute(self.grid, (5, 5), (0, 0), (0, 5), self.segments)
        for key, entry in data["masks"].items():
            if not entry["ok"]:
                continue
            problems = dual.verify_paths(
                entry["flows"], data["rows"], data["cols"],
                (5, 5), (0, 0), (0, 5),
                [tuple(c) for c in entry["path_a"]],
                [tuple(c) for c in entry["path_b"]],
            )
            self.assertEqual(problems, [], f"mask {key}")

    def test_select_respects_confirmations(self):
        data = dual.precompute(self.grid, (5, 5), (0, 0), (0, 5), self.segments)
        names = data["segment_names"]
        # 强制全部测段使用备选值；备选值与快照值一致，所以仍然可行。
        confirmed = {name: True for name in names}
        mask = dual.select_mask(data, confirmed)
        self.assertEqual(mask, (1 << len(names)) - 1)
        # 强制一个不可行的组合（把沟口备选值抬到高于来水格）后应找不到方案。
        segments = {k: dict(v) for k, v in self.segments.items()}
        segments["沟口"] = {(5, 5): 9999}
        data2 = dual.precompute(self.grid, (5, 5), (0, 0), (0, 5), segments)
        self.assertIsNone(dual.select_mask(data2, {"沟口": True}))

    def test_effective_grid_applies_mask_per_segment(self):
        names = sorted(self.segments)
        idx = names.index("沟口")
        grid = dual.effective_grid(self.grid, names, self.segments, 1 << idx)
        self.assertEqual(grid[5][5], 75)
        # 其它测段未取备选值 -> 保持快照值。
        self.assertEqual(grid[0][0], self.grid[0][0])

    def test_no_dual_route_reports_not_found(self):
        # 沟口放在一个无法从两路到达的角落
        data = dual.precompute(self.grid, (0, 0), (0, 0), (0, 5), self.segments)
        self.assertIsNone(data["suggested"])
        self.assertTrue(all(not v["ok"] for v in data["masks"].values()))

    def test_flow_encoding_roundtrip(self):
        flows = flow_sets(self.grid)
        encoded = dual.encode_flows(flows)
        decoded = dual.decode_flows(encoded, len(self.grid), len(self.grid[0]))
        self.assertEqual(decoded, flows)


if __name__ == "__main__":
    unittest.main()
