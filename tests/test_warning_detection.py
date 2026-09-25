"""预警检测的去重、更新与重开规则测试。

覆盖：基准线升降、不同指标、重复运行、新届次基准线导入、
人工关闭状态、连续下降区间扩展以及检测接口的计数返回。
"""

import shutil
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    Base,
    College,
    Graduate,
    ProvinceReferenceLine,
    Warning,
    DestinationStatus,
    DestinationType,
    WarningLevel,
    WarningStatus,
    WarningType,
)
from app.utils.warning_detector import (
    run_warning_detection_for_target,
    run_full_warning_detection,
)
from app.api.warnings import run_detection


class WarningDetectionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.engine = create_engine(
            f"sqlite:///{self.tmpdir}/test.db",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self._seq = 0

        self.college = College(name="测试学院", code="T001")
        self.db.add(self.college)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---------- 数据构造 ----------

    def _add_graduates(self, year, confirmed_of_10, aligned_of_employed=0):
        """写入一届 10 名毕业生，confirmed_of_10 名为已落实就业。"""
        for i in range(10):
            self._seq += 1
            confirmed = i < confirmed_of_10
            self.db.add(Graduate(
                student_id=f"S{self._seq:06d}",
                name=f"学生{self._seq}",
                major="计算机科学",
                graduation_year=year,
                college_id=self.college.id,
                has_micro_major=False,
                destination_status=(
                    DestinationStatus.CONFIRMED if confirmed else DestinationStatus.PENDING
                ),
                destination_type=(
                    DestinationType.EMPLOYMENT if confirmed else DestinationType.UNDECIDED
                ),
                is_aligned=confirmed and i < aligned_of_employed,
            ))
        self.db.commit()

    def _add_graduates_batch(self, year, confirmed_of_50):
        """写入一届 50 名毕业生（用于构造连续下降届次）。"""
        for i in range(50):
            self._seq += 1
            confirmed = i < confirmed_of_50
            self.db.add(Graduate(
                student_id=f"S{self._seq:06d}",
                name=f"学生{self._seq}",
                major="计算机科学",
                graduation_year=year,
                college_id=self.college.id,
                has_micro_major=False,
                destination_status=(
                    DestinationStatus.CONFIRMED if confirmed else DestinationStatus.PENDING
                ),
                destination_type=(
                    DestinationType.EMPLOYMENT if confirmed else DestinationType.UNDECIDED
                ),
                is_aligned=False,
            ))
        self.db.commit()

    def _set_baseline(self, year, indicator, threshold):
        """导入（新增或更新）一条省级基准线。"""
        line = self.db.query(ProvinceReferenceLine).filter_by(
            graduation_year=year, indicator=indicator,
        ).first()
        if line is None:
            line = ProvinceReferenceLine(
                graduation_year=year,
                indicator=indicator,
                province_average=threshold,
                threshold=threshold,
            )
            self.db.add(line)
        else:
            line.threshold = threshold
        self.db.commit()
        return line

    def _warnings(self, indicator="confirmed_rate",
                  warning_type=WarningType.BELOW_PROVINCE_LINE, status=None):
        query = self.db.query(Warning).filter(
            Warning.target_type == "college",
            Warning.target_id == self.college.id,
            Warning.warning_type == warning_type,
            Warning.indicator == indicator,
        )
        if status is not None:
            query = query.filter(Warning.status == status)
        return query.all()

    # ---------- 基准线变化 ----------

    def test_baseline_raise_updates_existing_warning(self):
        """基准线上调：更新既有预警的依据、级别和差距，不新增预警。"""
        self._add_graduates(2024, 7)  # 落实率 70%
        self._set_baseline(2024, "confirmed_rate", 73.0)

        first = run_full_warning_detection(self.db)
        self.assertEqual(first["created"], 1)
        self.assertEqual(first["updated"], 0)
        warning = self._warnings()[0]
        self.assertEqual(warning.warning_level, WarningLevel.YELLOW)
        self.assertEqual(warning.province_value, 73.0)
        self.assertEqual(warning.gap, 3.0)

        self._set_baseline(2024, "confirmed_rate", 86.0)
        second = run_full_warning_detection(self.db)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["updated"], 1)

        warnings = self._warnings()
        self.assertEqual(len(warnings), 1)
        updated = warnings[0]
        self.assertEqual(updated.id, warning.id)
        self.assertEqual(updated.status, WarningStatus.ACTIVE)
        self.assertEqual(updated.warning_level, WarningLevel.RED)
        self.assertEqual(updated.province_value, 86.0)
        self.assertEqual(updated.gap, 16.0)

    def test_baseline_lower_updates_existing_warning(self):
        """基准线下调：既有预警的级别与差距随之下调。"""
        self._add_graduates(2024, 7)
        self._set_baseline(2024, "confirmed_rate", 86.0)
        run_full_warning_detection(self.db)
        warning = self._warnings()[0]
        self.assertEqual(warning.warning_level, WarningLevel.RED)

        self._set_baseline(2024, "confirmed_rate", 74.0)
        result = run_full_warning_detection(self.db)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 1)

        warnings = self._warnings()
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].warning_level, WarningLevel.YELLOW)
        self.assertEqual(warnings[0].province_value, 74.0)
        self.assertEqual(warnings[0].gap, 4.0)

        # 基准线继续下调到不再低于省线：不再产生检测结果，也不新增预警
        self._set_baseline(2024, "confirmed_rate", 69.0)
        result = run_full_warning_detection(self.db)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(self._warnings()), 1)

    def test_new_baseline_year_updates_instead_of_duplicating(self):
        """导入新届次基准线后再检测：同一学院同一指标仍只有一条活动预警。"""
        self._add_graduates(2024, 7)
        self._set_baseline(2024, "confirmed_rate", 73.0)
        run_full_warning_detection(self.db)
        warning = self._warnings()[0]
        self.assertEqual((warning.start_year, warning.end_year), (2024, 2024))

        # 新一届毕业生 + 导入新届次省级基准线
        self._add_graduates(2025, 7)
        self._set_baseline(2025, "confirmed_rate", 72.0)
        result = run_full_warning_detection(self.db)

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 1)
        warnings = self._warnings()
        self.assertEqual(len(warnings), 1)
        active = self._warnings(status=WarningStatus.ACTIVE)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].id, warning.id)
        self.assertEqual((active[0].start_year, active[0].end_year), (2024, 2025))
        self.assertEqual(active[0].current_value, 70.0)
        self.assertEqual(active[0].province_value, 72.0)
        self.assertEqual(active[0].gap, 2.0)

    # ---------- 重复运行 ----------

    def test_repeated_runs_are_idempotent(self):
        """数据不变时重复检测：第二次起全部跳过，不产生新预警。"""
        self._add_graduates(2024, 7)
        self._set_baseline(2024, "confirmed_rate", 73.0)

        first = run_full_warning_detection(self.db)
        self.assertEqual(first["created"], 1)

        for _ in range(2):
            result = run_full_warning_detection(self.db)
            self.assertEqual(result["created"], 0)
            self.assertEqual(result["updated"], 0)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(len(self._warnings()), 1)

        target_result = run_warning_detection_for_target(
            self.db, "college", self.college.id
        )
        self.assertEqual(target_result["created"], 0)
        self.assertEqual(target_result["skipped"], 1)
        self.assertEqual(len(self._warnings()), 1)

    def test_legacy_duplicate_actives_are_consolidated(self):
        """历史遗留的同键重复活动预警：检测后只保留一条活动状态。"""
        self._add_graduates(2024, 7)
        self._set_baseline(2024, "confirmed_rate", 73.0)
        run_full_warning_detection(self.db)

        # 模拟旧版本缺陷留下的重复活动预警
        for _ in range(2):
            self.db.add(Warning(
                warning_type=WarningType.BELOW_PROVINCE_LINE,
                warning_level=WarningLevel.YELLOW,
                status=WarningStatus.ACTIVE,
                target_type="college",
                target_id=self.college.id,
                target_name=self.college.name,
                indicator="confirmed_rate",
                current_value=70.0,
                province_value=73.0,
                gap=3.0,
                start_year=2024,
                end_year=2024,
                decline_count=1,
            ))
        self.db.commit()
        self.assertEqual(len(self._warnings(status=WarningStatus.ACTIVE)), 3)

        result = run_full_warning_detection(self.db)
        self.assertEqual(result["updated"], 1)

        active = self._warnings(status=WarningStatus.ACTIVE)
        self.assertEqual(len(active), 1)
        # 保留最近一条并更新为最新依据，其余转为已忽略
        self.assertEqual(active[0].province_value, 73.0)
        self.assertEqual(active[0].gap, 3.0)
        self.assertEqual(active[0].warning_level, WarningLevel.YELLOW)
        dismissed = self._warnings(status=WarningStatus.DISMISSED)
        self.assertEqual(len(dismissed), 2)

    # ---------- 不同指标 ----------

    def test_indicators_are_tracked_independently(self):
        """不同指标各自一条预警，一个指标的基准线变化不影响另一个。"""
        self._add_graduates(2024, 7, aligned_of_employed=2)  # 落实率70% 对口率28.57%
        self._set_baseline(2024, "confirmed_rate", 73.0)     # 差距 3 -> 黄
        self._set_baseline(2024, "aligned_rate", 38.0)       # 差距 9.43 -> 黄

        first = run_full_warning_detection(self.db)
        self.assertEqual(first["created"], 2)
        self.assertEqual(len(self._warnings("confirmed_rate")), 1)
        self.assertEqual(len(self._warnings("aligned_rate")), 1)

        self._set_baseline(2024, "confirmed_rate", 81.0)     # 差距 11 -> 橙
        second = run_full_warning_detection(self.db)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(second["skipped"], 1)

        confirmed_warning = self._warnings("confirmed_rate")[0]
        self.assertEqual(confirmed_warning.warning_level, WarningLevel.ORANGE)
        self.assertEqual(confirmed_warning.gap, 11.0)

        aligned_warning = self._warnings("aligned_rate")[0]
        self.assertEqual(aligned_warning.warning_level, WarningLevel.YELLOW)
        self.assertEqual(aligned_warning.province_value, 38.0)
        self.assertAlmostEqual(aligned_warning.gap, 9.43, places=2)

    # ---------- 关闭状态 ----------

    def test_resolved_warning_stays_closed_without_escalation(self):
        """人工解决的预警：规则未明确要求（级别未升级）时不得重新打开。"""
        self._add_graduates(2024, 7)
        self._set_baseline(2024, "confirmed_rate", 73.0)
        run_full_warning_detection(self.db)
        warning = self._warnings()[0]
        self.assertEqual(warning.warning_level, WarningLevel.YELLOW)

        warning.status = WarningStatus.RESOLVED
        self.db.commit()

        # 数据不变再检测：保持已解决，不新建
        result = run_full_warning_detection(self.db)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(self._warnings()), 1)
        self.assertEqual(self._warnings()[0].status, WarningStatus.RESOLVED)
        self.assertEqual(len(self._warnings(status=WarningStatus.ACTIVE)), 0)

        # 基准线小幅变化但级别未升级：仍不得重开，也不改动已解决记录
        self._set_baseline(2024, "confirmed_rate", 74.0)
        result = run_full_warning_detection(self.db)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"], 1)
        closed = self._warnings()[0]
        self.assertEqual(closed.status, WarningStatus.RESOLVED)
        self.assertEqual(closed.province_value, 73.0)

    def test_resolved_warning_reopens_only_on_level_escalation(self):
        """人工解决的预警：新检测级别高于关闭时级别才重新打开。"""
        self._add_graduates(2024, 7)
        self._set_baseline(2024, "confirmed_rate", 73.0)
        run_full_warning_detection(self.db)
        warning = self._warnings()[0]

        warning.status = WarningStatus.RESOLVED
        self.db.commit()

        # 基准线上调使差距 3 -> 11，级别黄 -> 橙，规则明确要求重开
        self._set_baseline(2024, "confirmed_rate", 81.0)
        result = run_full_warning_detection(self.db)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 1)

        warnings = self._warnings()
        self.assertEqual(len(warnings), 1)
        reopened = warnings[0]
        self.assertEqual(reopened.id, warning.id)
        self.assertEqual(reopened.status, WarningStatus.ACTIVE)
        self.assertEqual(reopened.warning_level, WarningLevel.ORANGE)
        self.assertEqual(reopened.province_value, 81.0)
        self.assertEqual(reopened.gap, 11.0)

    # ---------- 连续下降预警 ----------

    def test_decline_warning_extends_without_duplicate(self):
        """连续下降区间随新届次扩展：更新既有预警而非新增。"""
        self._add_graduates_batch(2022, 40)  # 80%
        self._add_graduates_batch(2023, 39)  # 78%
        self._add_graduates_batch(2024, 38)  # 76%，连续 2 届下降 -> 黄

        first = run_full_warning_detection(self.db)
        self.assertEqual(first["created"], 1)
        warning = self._warnings(warning_type=WarningType.CONFIRMED_RATE_DECLINE)[0]
        self.assertEqual(warning.warning_level, WarningLevel.YELLOW)
        self.assertEqual((warning.start_year, warning.end_year), (2022, 2024))
        self.assertEqual(warning.decline_count, 2)

        self._add_graduates_batch(2025, 37)  # 74%，连续 3 届下降 -> 橙
        second = run_full_warning_detection(self.db)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["updated"], 1)

        warnings = self._warnings(warning_type=WarningType.CONFIRMED_RATE_DECLINE)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].id, warning.id)
        self.assertEqual(warnings[0].warning_level, WarningLevel.ORANGE)
        self.assertEqual((warnings[0].start_year, warnings[0].end_year), (2022, 2025))
        self.assertEqual(warnings[0].decline_count, 3)

    # ---------- 接口返回 ----------

    def test_detect_endpoint_returns_outcome_counts(self):
        """检测接口返回本次新增、更新和跳过的数量。"""
        self._add_graduates(2024, 7)
        self._set_baseline(2024, "confirmed_rate", 73.0)

        first = run_detection(target_type=None, target_id=None, db=self.db)
        self.assertEqual(first["created"], 1)
        self.assertEqual(first["updated"], 0)
        self.assertEqual(first["skipped"], 0)
        self.assertEqual(first["total_warnings"], 1)

        second = run_detection(target_type=None, target_id=None, db=self.db)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(len(self._warnings()), 1)

        targeted = run_detection(
            target_type="college", target_id=self.college.id, db=self.db
        )
        self.assertEqual(targeted["created"], 0)
        self.assertEqual(targeted["updated"], 0)
        self.assertEqual(targeted["skipped"], 1)
        self.assertEqual(targeted["warnings_count"], 0)

        self._set_baseline(2024, "confirmed_rate", 81.0)
        third = run_detection(
            target_type="college", target_id=self.college.id, db=self.db
        )
        self.assertEqual(third["created"], 0)
        self.assertEqual(third["updated"], 1)
        self.assertEqual(third["warnings_count"], 1)
        self.assertEqual(len(self._warnings()), 1)


if __name__ == "__main__":
    unittest.main()
