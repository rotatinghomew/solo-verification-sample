"""输入资料的契约核对（在原始演练测试之外补充业务一致性检查）。"""

import json
import unittest
from pathlib import Path

from audit.validation import load_payload, validate_payload

ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "data" / "records.json"


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(RECORDS.read_text(encoding="utf-8"))

    def test_validator_accepts_bundle(self):
        report = validate_payload(self.payload)
        self.assertEqual(report.errors, [])
        self.assertTrue(report.ok)

    def test_loader_roundtrip(self):
        payload = load_payload(RECORDS)
        self.assertEqual(len(payload["frame"]), 288)

    def test_random_freeze_nn_fixed_at_issue(self):
        # 同一冻结版本内 n、N 必须对所有随机抽取行一致，且抽取行数等于 n
        by_freeze = {}
        for row in self.payload["draws"]:
            if row["mode"] != "预先随机":
                continue
            by_freeze.setdefault(row["freeze"], []).append(row)
        for freeze, rows in by_freeze.items():
            n_n = {(r["n"], r["N"]) for r in rows}
            self.assertEqual(len(n_n), 1, freeze)
            n, _ = n_n.pop()
            self.assertEqual(n, len(rows), freeze)

    def test_every_result_belongs_to_a_draw_and_outcomes_known(self):
        draw_keys = {(d["week"], d["stratum"], d["unit"], d["mode"]) for d in self.payload["draws"]}
        units = {d["unit"] for d in self.payload["draws"]}
        allowed = {"到场", "未到场", "失联", "拒绝出证"}
        for r in self.payload["results"]:
            self.assertIn(r["unit"], units)
            self.assertIn(r["outcome"], allowed)

    def test_revisions_reference_known_segments(self):
        frame_units = {f["segment"] for f in self.payload["frame"]}
        for rev in self.payload["revisions"]:
            if rev["kind"] == "重复申报":
                self.assertIn(rev["unit"], frame_units)
                self.assertIn(rev["canonical"], frame_units)
            else:
                # 后补单位在冻结总框之外
                self.assertNotIn(rev["unit"], frame_units)

    def test_staff_slots_non_negative(self):
        for s in self.payload["staff"]:
            self.assertGreaterEqual(s["slots"], 0)


if __name__ == "__main__":
    unittest.main()
