"""audit —— 校外体验参与登记的分层抽样、滚动复核与发布门禁分析包。

模块划分
--------
- validation : 输入契约核对
- frames     : 冻结总体、修订（后补/重复申报）与适用总体版本
- sampling   : 有效抽样机会、随机/指定口径与补抽排程
- estimation : 加权比例、无应答未知区间、整群相关与发布判定
- report     : 汇总为可给编辑看的结构化结果
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KNOWN_OUTCOMES = frozenset({"到场", "未到场", "失联", "拒绝出证"})
# 只有这两种结局给出可估计的出勤证据；另两种按无应答处理（影响保留未知）
EVIDENCE_OUTCOMES = frozenset({"到场", "未到场"})
RANDOM_MODE = "预先随机"
TARGETED_MODE = "指定核查"


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_payload(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as fh:
        return json.load(fh)


def validate_payload(payload: dict[str, Any]) -> ValidationReport:
    """核对资料字典里声明的关联关系；错误会阻止后续解释。"""
    report = ValidationReport()
    required = {"frame", "draws", "results", "revisions", "staff", "release"}
    missing = required - payload.keys()
    if missing:
        report.errors.append(f"缺少顶层字段: {sorted(missing)}")
        return report

    frame_units = {row["segment"] for row in payload["frame"]}
    if len(frame_units) != len(payload["frame"]):
        report.errors.append("frame 中存在重复 segment")

    # 抽取：随机行带固定 n/N（普通冻结 0<n<N；补抽池允许 n=N 的全数核查）
    freeze_nn: dict[str, set[tuple[int, int]]] = {}
    random_counts: dict[str, int] = {}
    drawn_units: set[str] = set()
    for row in payload["draws"]:
        drawn_units.add(row["unit"])
        if row["mode"] == RANDOM_MODE:
            is_supplement = "补抽" in row["freeze"]
            upper_ok = row["n"] <= row["N"] if is_supplement else row["n"] < row["N"]
            if not row.get("n") or not row.get("N") or not (0 < row["n"] and upper_ok):
                report.errors.append(f"随机抽取 {row['unit']} 的 n/N 不合法: {row.get('n')}/{row.get('N')}")
                continue
            freeze_nn.setdefault(row["freeze"], set()).add((row["n"], row["N"]))
            random_counts[row["freeze"]] = random_counts.get(row["freeze"], 0) + 1
            # 补抽单位来自后补池，允许不在原始 frame；普通 freeze 必须在框内
            if not is_supplement and row["unit"] not in frame_units:
                report.errors.append(f"随机抽取单位 {row['unit']} 不在冻结总框内")
        elif row["mode"] == TARGETED_MODE:
            if row["n"] is not None or row["N"] is not None:
                report.errors.append(f"指定核查 {row['unit']} 不应携带随机 n/N")
            if row["unit"] not in frame_units:
                report.errors.append(f"指定核查单位 {row['unit']} 不在总框内")
        else:
            report.errors.append(f"未知抽取模式: {row['mode']} ({row['unit']})")
    for freeze, nn in freeze_nn.items():
        if len(nn) > 1:
            report.errors.append(f"冻结版本 {freeze} 内 n/N 不一致: {nn}")
        else:
            (n, _), = tuple(nn)
            if random_counts[freeze] != n:
                report.errors.append(
                    f"冻结版本 {freeze} 随机行数 {random_counts[freeze]} 与声明 n={n} 不符"
                )

    # 回执必须挂在已抽取单位上，结局取值受控，一个单位至多一条
    seen_results: set[str] = set()
    for row in payload["results"]:
        if row["unit"] not in drawn_units:
            report.errors.append(f"回执单位 {row['unit']} 没有对应抽取记录")
        if row["outcome"] not in KNOWN_OUTCOMES:
            report.errors.append(f"回执 {row['unit']} 结局未知: {row['outcome']}")
        if row["unit"] in seen_results:
            report.errors.append(f"单位 {row['unit']} 出现多条回执（不得当作多次独立抽样）")
        seen_results.add(row["unit"])

    # 修订：重复申报指向同框 canonical；后补单位在框外（被补抽 freeze 覆盖除外）
    supplement_units = {
        r["unit"] for r in payload["draws"]
        if r["mode"] == RANDOM_MODE and "补抽" in r["freeze"]
    }
    for rev in payload["revisions"]:
        if rev["kind"] == "重复申报":
            if rev["unit"] not in frame_units or rev["canonical"] not in frame_units:
                report.errors.append(f"重复申报映射不可证实: {rev}")
            if rev["unit"] == rev["canonical"]:
                report.errors.append(f"重复申报自指: {rev['unit']}")
        elif rev["kind"] == "后补":
            if rev["unit"] in frame_units and rev["unit"] not in supplement_units:
                report.errors.append(f"后补单位 {rev['unit']} 已存在于冻结总框")
        else:
            report.errors.append(f"未知修订类型: {rev['kind']}")

    # 发布契约
    rel = payload["release"]
    for key in ("published_through", "confidence", "groups", "max_width",
                "minimum_verified", "valid_frame_weeks"):
        if key not in rel:
            report.errors.append(f"release 缺少字段 {key}")
    if not 0 < rel.get("confidence", 0) < 1:
        report.errors.append("release.confidence 必须落在 (0,1)")
    if rel.get("max_width", 0) <= 0:
        report.errors.append("release.max_width 必须为正")
    if rel.get("minimum_verified", 0) <= 0:
        report.errors.append("release.minimum_verified 必须为正")

    # 访员时段
    for row in payload["staff"]:
        if row["slots"] < 0:
            report.errors.append(f"访员 {row['investigator']} 时段为负")

    return report
