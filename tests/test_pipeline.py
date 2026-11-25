"""业务口径的回归测试：把抽样/复核/门禁规则固化成断言。"""

import unittest
from pathlib import Path

from audit.frames import build_ledger
from audit.report import run_pipeline
from audit.sampling import build_sample, plan_supplements
from audit.scenarios import _payload_with_scenario, run_scenario
from audit.validation import load_payload, validate_payload

ROOT = Path(__file__).resolve().parents[1]


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = load_payload(ROOT / "data" / "records.json")
        cls.result = run_pipeline(cls.payload)

    def test_contract_passes(self):
        self.assertTrue(validate_payload(self.payload).ok)

    # ---- 总体版本与重复/后补 ----
    def test_week34_frame_versions(self):
        ledger = build_ledger(self.payload)
        for stratum in ("东-活动", "南-活动", "内-走读", "东-走读"):
            bucket = ledger.frames[(34, stratum)]
            self.assertEqual(bucket.freeze_n, 24)
            self.assertEqual(bucket.current_n, 24)  # -1 重复 +1 后补
            self.assertEqual(len(bucket.late_units), 1)
            self.assertEqual(len(bucket.duplicates), 1)
            # 后补单位在旧抽取下机会为 0
            late = next(iter(bucket.late_units))
            self.assertEqual(bucket.frozen_coverage(late), 0.0)
            self.assertIn(late, bucket.uncovered_units())

    def test_duplicate_physical_segment_coverage_uses_merged_rows(self):
        ledger = build_ledger(self.payload)
        bucket = ledger.frames[(34, "南-活动")]
        # canonical 16 吸收重复行 17（两行都在冻结框），覆盖概率按 k=2 物理段计算
        p16 = bucket.frozen_coverage("34-南-活动-16")
        # 单行单位的机会 = n/N = 7/24
        p_single = bucket.frozen_coverage("34-南-活动-15")
        self.assertAlmostEqual(p_single, 7 / 24)
        self.assertGreater(p16, p_single)  # 两行任一命中即覆盖，机会更高
        # 与组合公式一致：1 - C(22,7)/C(24,7)
        from math import comb
        self.assertAlmostEqual(p16, 1 - comb(22, 7) / comb(24, 7))

    # ---- 随机/指定口径 ----
    def test_targeted_only_units_excluded_from_random_estimate(self):
        registry = build_sample(build_ledger(self.payload), self.payload)
        sample = registry[(32, "东-活动")]
        targeted = sample.targeted_only_units()
        # 该层指定单位 23 同时也在随机名单时不算 targeted_only；纯指定才剔除
        for u in targeted:
            self.assertNotIn(u, sample.random_units())
        # 所有随机单位的结局只来自 draws 登记，且指定核查不改变 n/N
        for u in sample.random_units():
            self.assertTrue(u.in_random)

    def test_dual_identity_unit_keeps_random_status(self):
        registry = build_sample(build_ledger(self.payload), self.payload)
        sample = registry[(34, "东-活动")]
        by_unit = {u.unit: u for u in sample.units}
        # 23 同时被随机和指定命中：保留随机身份，不是 targeted_only
        self.assertTrue(by_unit["34-东-活动-23"].in_random)
        self.assertFalse(by_unit["34-东-活动-23"].targeted_only)

    def test_duplicate_drawn_row_maps_to_canonical_single_count(self):
        registry = build_sample(build_ledger(self.payload), self.payload)
        sample = registry[(34, "南-活动")]
        eff = {u.effective_segment for u in sample.random_units()}
        # 17 是 16 的重复申报：物理段只算一次
        self.assertIn("34-南-活动-16", eff)
        self.assertNotIn("34-南-活动-17", eff)
        # 该层有效随机单位 = 7 抽 - 1 重复折并 = 6
        self.assertEqual(len(eff), 6)

    # ---- 无应答三种结局区分 ----
    def test_outcomes_kept_distinct_and_nonresponse_not_imputed(self):
        est = next(e for e in self.result.stratum_estimates
                   if (e.week, e.stratum) == (32, "东-活动"))
        # 2 确证（1 到场 1 未到场），5 无应答（失联/拒绝出证），0 待核
        self.assertEqual(est.evidence_n, 2)
        self.assertEqual(est.verified_attended, 1)
        self.assertEqual(est.nonresponse_n, 5)
        self.assertEqual(est.pending_n, 0)
        # 未知份额 = 5/7，未插补，区间因此打到边界
        self.assertAlmostEqual(est.unknown_share, 5 / 7)
        self.assertEqual(est.ci_low, 0.0)
        self.assertEqual(est.ci_high, 1.0)

    # ---- 排程：已约不可取消、站点约束、补抽优先 ----
    def test_schedule_reserves_appointments_and_respects_site(self):
        ledger = build_ledger(self.payload)
        registry = build_sample(ledger, self.payload)
        schedule = plan_supplements(ledger, registry, self.payload, 35)
        # 内站点 1 时段但有 2 个已约：一个顺延，没有补抽余量
        nei = schedule.plans["内"]
        self.assertEqual(nei.total_slots, 1)
        self.assertEqual(nei.residual_slots, 0)
        self.assertEqual(nei.deferred_appointments, ["34-内-走读-23"])
        # 东 5 时段、4 已约 -> 1 补抽；南 2 时段、2 已约 -> 0
        self.assertEqual(schedule.plans["东"].residual_slots, 1)
        self.assertEqual(schedule.plans["南"].residual_slots, 0)
        # 补抽只安排本点单位
        self.assertTrue(all("东" in u for u in schedule.plans["东"].scheduled))
        # 南、内的后补单位排不上
        self.assertIn("34-南-活动-24", schedule.plans["南"].unscheduled)
        self.assertIn("34-内-走读-24", schedule.plans["内"].unscheduled)

    def test_frozen_probabilities_never_rewritten(self):
        # 情景追加补抽后，原 freeze 的 n/N 与单位集合保持不变
        sp = _payload_with_scenario(self.payload, {"34-东-活动-24": "到场"},
                                    ["34-东-活动-24"])
        key = lambda d: (d["unit"], str(d["n"]), str(d["N"]), d["freeze"])
        orig_freeze = [d for d in self.payload["draws"] if "补抽" not in d["freeze"]]
        new_freeze = [d for d in sp["draws"] if "补抽" not in d["freeze"]]
        self.assertEqual(
            sorted((key(d) for d in orig_freeze)),
            sorted((key(d) for d in new_freeze)),
        )
        # 新增的只有补抽 freeze
        added = [d for d in sp["draws"] if d not in self.payload["draws"]]
        self.assertTrue(all("补抽" in d["freeze"] for d in added))

    # ---- 发布门禁 ----
    def test_published_weeks_not_reedited(self):
        for key, agg in self.result.aggregates.items():
            if key.startswith(("周32", "周33")):
                self.assertIn("历史已发布", agg.release_status)
                self.assertEqual(agg.blocking_reasons, [])

    def test_current_groups_blocked_by_coverage_and_width(self):
        for g in ("组:活动", "组:走读"):
            agg = self.result.aggregates[g]
            self.assertEqual(agg.release_status, "暂不能采用")
            self.assertTrue(any("未覆盖" in r for r in agg.blocking_reasons))
            self.assertTrue(any("宽度" in r for r in agg.blocking_reasons))

    def test_minimum_verified_gate(self):
        # 造一个最低确证被抬高的契约，确认门禁拦截
        import copy
        rel = copy.deepcopy(self.payload["release"])
        rel["minimum_verified"] = 1000
        p = copy.deepcopy(self.payload)
        p["release"] = rel
        res = run_pipeline(p)
        agg = res.aggregates["总体"]
        self.assertTrue(any("最低确证" in r for r in agg.blocking_reasons))

    # ---- 情景：补抽关闭覆盖缺口，且可复算 ----
    def test_scenario_full_coverage_closes_gap_for_east_activity(self):
        sc = run_scenario(
            self.payload, "t", "t",
            {"34-东-活动-24": "到场"}, ["34-东-活动-24"],
        )
        # 情景资料自身通过契约
        sp = _payload_with_scenario(self.payload, {"34-东-活动-24": "到场"},
                                    ["34-东-活动-24"])
        self.assertTrue(validate_payload(sp).ok)
        # 周34 东-活动所在层覆盖回到 100%
        ledger = build_ledger(sp)
        bucket = ledger.frames[(34, "东-活动")]
        self.assertEqual(bucket.frozen_coverage("34-东-活动-24"), 1.0)  # 池规模1全数核查
        self.assertEqual(bucket.uncovered_units(), set())
        # 但走读组仍有未覆盖（东-走读后补排不上）
        zoudou = sc.aggregates["周34:走读"]
        self.assertGreater(zoudou.uncovered_n, 0)

    def test_scenario_includes_ideal_full_closure(self):
        names = [s.name for s in self.result.scenarios]
        self.assertTrue(any("理想情景" in n for n in names))
        ideal = next(s for s in self.result.scenarios if "理想情景" in s.name)
        # 理想情景下总体覆盖率回到 100%
        self.assertAlmostEqual(ideal.aggregates["总体"].covered_fraction, 1.0)

    # ---- 整群相关：共享活动跨层 ----
    def test_cluster_pairing_across_strata(self):
        ledger = build_ledger(self.payload)
        # 每个活动由两个层各 3 段共享
        seen = {}
        for bucket in ledger.frames.values():
            for u in bucket.frozen_units.values():
                if u.shared_activity:
                    seen.setdefault(u.shared_activity, set()).add(bucket.stratum)
        for activity, strata in seen.items():
            self.assertEqual(len(strata), 2, activity)

    # ---- 无应答补约访：π 不变、只降未知份额 ----
    def test_followup_inventory_and_probability_invariance(self):
        followups = self.result.nonresponse_followup_units
        # 36 个已抽中无应答随机单位，结局只含失联/拒绝出证
        self.assertEqual(len(followups), 36)
        self.assertTrue(all(r["outcome"] in ("失联", "拒绝出证") for r in followups))
        # 补约访情景不改任何 freeze，只改回执
        target = next(r["unit"] for r in followups
                      if r["week"] == 34 and r["stratum"] == "东-活动")
        sp = _payload_with_scenario(self.payload, {target: "到场"}, [])
        self.assertEqual(
            [d for d in sp["draws"] if "补抽" in d["freeze"]], []
        )
        # 该单位结局变为到场后，其层未知份额下降、确证数上升
        from audit.scenarios import followup_scenario
        sc = followup_scenario(
            self.payload, "t", "t", {target: "到场"},
        )
        a_before = self.result.aggregates["周34:活动"]
        a_after = sc.aggregates["周34:活动"]
        self.assertGreaterEqual(a_after.evidence_n, a_before.evidence_n)

    def test_demo_scenario_distinguishes_width_and_coverage_gates(self):
        demo = next(s for s in self.result.scenarios if "追加时段" in s.name)
        # 东-活动宽度障碍解除（<0.28），但南点后补未覆盖仍拦截整组
        a = demo.aggregates["周34:活动"]
        self.assertLess(a.interval_width, self.result.release["max_width"])
        self.assertGreater(a.uncovered_n, 0)
        self.assertEqual(a.release_status, "暂不能采用")


if __name__ == "__main__":
    unittest.main()
