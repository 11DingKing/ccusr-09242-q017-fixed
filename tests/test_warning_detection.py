"""预警检测的去重与更新条件测试。

覆盖：基准线升降、不同指标、重复运行、关闭状态（人工解决后的重新打开规则），
以及检测接口返回的新增/更新/跳过数量。
"""

import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.warnings import run_detection
from app.models import (
    Base,
    College,
    Graduate,
    MicroMajor,
    ProvinceReferenceLine,
    Warning,
    WarningLevel,
    WarningStatus,
    WarningType,
)
from app.models.enums import DestinationStatus, DestinationType
from app.utils.warning_detector import (
    run_full_warning_detection,
    run_warning_detection_for_target,
)


def make_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def add_college(db, code="CS", name="计算机学院"):
    college = College(name=name, code=code)
    db.add(college)
    db.flush()
    return college


def add_graduates(db, college_id, year, confirmed, total, aligned=None):
    """写入一届毕业生，confirmed/total 决定去向落实率，aligned 决定对口率。"""
    aligned = confirmed if aligned is None else aligned
    for i in range(total):
        is_confirmed = i < confirmed
        db.add(Graduate(
            student_id=f"{college_id}-{year}-{i}",
            name=f"学生{year}-{i}",
            major="计算机",
            graduation_year=year,
            college_id=college_id,
            destination_status=(
                DestinationStatus.CONFIRMED if is_confirmed else DestinationStatus.PENDING
            ),
            destination_type=(
                DestinationType.EMPLOYMENT if is_confirmed else DestinationType.UNDECIDED
            ),
            is_aligned=bool(is_confirmed and i < aligned),
        ))
    db.flush()


def add_reference_line(db, year, indicator, threshold, average=None):
    line = ProvinceReferenceLine(
        graduation_year=year,
        indicator=indicator,
        province_average=average if average is not None else threshold + 5,
        threshold=threshold,
    )
    db.add(line)
    db.flush()
    return line


def active_warnings(db, target_id, indicator, warning_type=WarningType.BELOW_PROVINCE_LINE):
    return db.query(Warning).filter(
        Warning.target_type == "college",
        Warning.target_id == target_id,
        Warning.warning_type == warning_type,
        Warning.indicator == indicator,
        Warning.status == WarningStatus.ACTIVE,
    ).all()


def all_warnings(db, target_id, indicator, warning_type=WarningType.BELOW_PROVINCE_LINE):
    return db.query(Warning).filter(
        Warning.target_type == "college",
        Warning.target_id == target_id,
        Warning.warning_type == warning_type,
        Warning.indicator == indicator,
    ).all()


class BelowProvinceLineDetectionTests(unittest.TestCase):
    """低于全省对照线预警的更新与去重。"""

    def setUp(self):
        self.db = make_session()
        self.college = add_college(self.db)
        # 2024 届落实率 70%，对口率 3/7 ≈ 42.86%
        add_graduates(self.db, self.college.id, 2024, confirmed=7, total=10, aligned=3)
        self.line = add_reference_line(self.db, 2024, "confirmed_rate", 73.0)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def detect(self):
        return run_full_warning_detection(self.db)

    def test_first_run_creates_warning(self):
        result = self.detect()
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(result["skipped_count"], 0)

        warning = active_warnings(self.db, self.college.id, "confirmed_rate")[0]
        self.assertEqual(warning.warning_level, WarningLevel.YELLOW)
        self.assertEqual(warning.current_value, 70.0)
        self.assertEqual(warning.province_value, 73.0)
        self.assertEqual(warning.gap, 3.0)

    def test_baseline_rise_updates_level_and_gap(self):
        """基准线上调：既有预警的依据、级别和差距被更新，而不是新增。"""
        self.detect()
        self.line.threshold = 80.0
        self.db.commit()

        result = self.detect()
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["updated_count"], 1)

        warnings = all_warnings(self.db, self.college.id, "confirmed_rate")
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning.status, WarningStatus.ACTIVE)
        self.assertEqual(warning.warning_level, WarningLevel.ORANGE)
        self.assertEqual(warning.province_value, 80.0)
        self.assertEqual(warning.gap, 10.0)

    def test_baseline_drop_downgrades_level(self):
        """基准线下调：级别随之降低，仍更新同一条预警。"""
        self.line.threshold = 80.0
        self.db.commit()
        self.detect()
        warning = active_warnings(self.db, self.college.id, "confirmed_rate")[0]
        self.assertEqual(warning.warning_level, WarningLevel.ORANGE)

        self.line.threshold = 74.0
        self.db.commit()
        result = self.detect()
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["updated_count"], 1)

        warnings = all_warnings(self.db, self.college.id, "confirmed_rate")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].warning_level, WarningLevel.YELLOW)
        self.assertEqual(warnings[0].gap, 4.0)

    def test_new_baseline_year_updates_instead_of_duplicating(self):
        """导入新一届基准线后再次全量检测：更新既有预警，不产生第二条活动预警。"""
        self.detect()
        # 新一届数据与基准线
        add_graduates(self.db, self.college.id, 2025, confirmed=7, total=10)
        add_reference_line(self.db, 2025, "confirmed_rate", 72.0)
        self.db.commit()

        result = self.detect()
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["updated_count"], 1)

        warnings = all_warnings(self.db, self.college.id, "confirmed_rate")
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning.status, WarningStatus.ACTIVE)
        self.assertEqual(warning.end_year, 2025)
        self.assertEqual(warning.province_value, 72.0)
        # 历史届次保留在明细中
        detail_years = [d["year"] for d in json.loads(warning.decline_details)]
        self.assertEqual(detail_years, [2024, 2025])

    def test_repeated_runs_are_idempotent(self):
        """重复运行不产生重复预警，第二次全部跳过。"""
        self.detect()
        result = self.detect()
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(len(all_warnings(self.db, self.college.id, "confirmed_rate")), 1)

    def test_indicators_are_tracked_independently(self):
        """不同指标各自维护一条预警，互不影响。"""
        add_reference_line(self.db, 2024, "aligned_rate", 60.0)
        self.db.commit()

        result = self.detect()
        self.assertEqual(result["created_count"], 2)

        confirmed = active_warnings(self.db, self.college.id, "confirmed_rate")[0]
        aligned = active_warnings(self.db, self.college.id, "aligned_rate")[0]
        self.assertNotEqual(confirmed.id, aligned.id)

        # 只调整对口就业率基准线：一个更新，一个跳过
        self.db.query(ProvinceReferenceLine).filter_by(
            graduation_year=2024, indicator="aligned_rate"
        ).first().threshold = 70.0
        self.db.commit()
        result = self.detect()
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(result["skipped_count"], 1)

    def test_resolved_warning_not_reopened_by_same_rule(self):
        """人工解决后，规则未升级时新一轮检测不重新打开、不新建。"""
        self.detect()
        warning = active_warnings(self.db, self.college.id, "confirmed_rate")[0]
        warning.status = WarningStatus.RESOLVED
        self.db.commit()

        # 基准线小幅变化但级别不变（仍为黄色）
        self.line.threshold = 74.0
        self.db.commit()
        result = self.detect()
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(result["skipped_count"], 1)

        warnings = all_warnings(self.db, self.college.id, "confirmed_rate")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].status, WarningStatus.RESOLVED)
        self.assertEqual(len(active_warnings(self.db, self.college.id, "confirmed_rate")), 0)

    def test_resolved_warning_reopened_only_on_escalation(self):
        """人工解决后，级别升级（规则明确要求）才重新打开同一条预警。"""
        self.detect()
        warning = active_warnings(self.db, self.college.id, "confirmed_rate")[0]
        warning.status = WarningStatus.RESOLVED
        self.db.commit()

        # 基准线上调使差距从 3 升到 10，黄色 -> 橙色
        self.line.threshold = 80.0
        self.db.commit()
        result = self.detect()
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["updated_count"], 1)

        warnings = all_warnings(self.db, self.college.id, "confirmed_rate")
        self.assertEqual(len(warnings), 1)
        reopened = warnings[0]
        self.assertEqual(reopened.id, warning.id)
        self.assertEqual(reopened.status, WarningStatus.ACTIVE)
        self.assertEqual(reopened.warning_level, WarningLevel.ORANGE)
        self.assertEqual(reopened.province_value, 80.0)
        self.assertEqual(reopened.gap, 10.0)

    def test_duplicate_active_warnings_are_consolidated(self):
        """历史遗留的重复活动预警在下一轮检测中被合并，只保留一条活动。"""
        self.detect()
        primary = active_warnings(self.db, self.college.id, "confirmed_rate")[0]
        duplicate = Warning(
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
            description="重复预警",
        )
        self.db.add(duplicate)
        self.db.commit()

        result = self.detect()
        self.assertEqual(result["updated_count"], 1)

        remaining = active_warnings(self.db, self.college.id, "confirmed_rate")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].id, primary.id)
        self.db.refresh(duplicate)
        self.assertEqual(duplicate.status, WarningStatus.RESOLVED)


class DeclineDetectionTests(unittest.TestCase):
    """连续下降预警的窗口更新。"""

    def setUp(self):
        self.db = make_session()
        self.college = add_college(self.db)

    def tearDown(self):
        self.db.close()

    def test_extended_decline_updates_existing_warning(self):
        """下降窗口延伸时更新既有预警，而不是新增一条。"""
        # 落实率 90 -> 80 -> 70，连续两届下降触发黄色
        add_graduates(self.db, self.college.id, 2022, confirmed=9, total=10)
        add_graduates(self.db, self.college.id, 2023, confirmed=8, total=10)
        add_graduates(self.db, self.college.id, 2024, confirmed=7, total=10)
        self.db.commit()

        result = run_full_warning_detection(self.db)
        self.assertEqual(result["created_count"], 1)
        warning = active_warnings(
            self.db, self.college.id, "confirmed_rate", WarningType.CONFIRMED_RATE_DECLINE
        )[0]
        self.assertEqual(warning.end_year, 2024)
        self.assertEqual(warning.decline_count, 2)

        # 新一届继续下降，窗口延伸、级别升级
        add_graduates(self.db, self.college.id, 2025, confirmed=6, total=10)
        self.db.commit()
        result = run_full_warning_detection(self.db)
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["updated_count"], 1)

        warnings = all_warnings(
            self.db, self.college.id, "confirmed_rate", WarningType.CONFIRMED_RATE_DECLINE
        )
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].end_year, 2025)
        self.assertEqual(warnings[0].decline_count, 3)
        self.assertEqual(warnings[0].warning_level, WarningLevel.ORANGE)


class DetectionApiTests(unittest.TestCase):
    """检测接口返回本次新增、更新和跳过的数量。"""

    def setUp(self):
        self.db = make_session()
        self.college = add_college(self.db)
        add_graduates(self.db, self.college.id, 2024, confirmed=7, total=10)
        self.line = add_reference_line(self.db, 2024, "confirmed_rate", 73.0)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_full_detection_returns_counts(self):
        response = run_detection(target_type=None, target_id=None, db=self.db)
        self.assertEqual(response["created_count"], 1)
        self.assertEqual(response["updated_count"], 0)
        self.assertEqual(response["skipped_count"], 0)

        self.line.threshold = 80.0
        self.db.commit()
        response = run_detection(target_type=None, target_id=None, db=self.db)
        self.assertEqual(response["created_count"], 0)
        self.assertEqual(response["updated_count"], 1)
        self.assertEqual(response["skipped_count"], 0)

        response = run_detection(target_type=None, target_id=None, db=self.db)
        self.assertEqual(response["created_count"], 0)
        self.assertEqual(response["updated_count"], 0)
        self.assertEqual(response["skipped_count"], 1)

    def test_target_detection_returns_counts_and_persists(self):
        response = run_detection(target_type="college", target_id=self.college.id, db=self.db)
        self.assertEqual(response["created_count"], 1)
        self.assertEqual(response["warnings_count"], 1)

        # 定向检测的结果已持久化
        self.assertEqual(len(active_warnings(self.db, self.college.id, "confirmed_rate")), 1)

        response = run_detection(target_type="college", target_id=self.college.id, db=self.db)
        self.assertEqual(response["created_count"], 0)
        self.assertEqual(response["skipped_count"], 1)

    def test_micro_major_detection_uses_same_rules(self):
        micro_major = MicroMajor(name="人工智能", code="MICRO-AI", college_id=self.college.id)
        self.db.add(micro_major)
        self.db.flush()
        for i in range(10):
            self.db.add(Graduate(
                student_id=f"mm-2024-{i}",
                name=f"微专业学生{i}",
                major="计算机",
                graduation_year=2024,
                college_id=self.college.id,
                has_micro_major=True,
                micro_major_id=micro_major.id,
                destination_status=(
                    DestinationStatus.CONFIRMED if i < 6 else DestinationStatus.PENDING
                ),
            ))
        self.db.commit()

        result = run_warning_detection_for_target(self.db, "micro_major", micro_major.id)
        self.assertEqual(len(result.created), 1)
        warning = result.created[0]
        self.assertEqual(warning.target_type, "micro_major")
        self.assertEqual(warning.current_value, 60.0)

        result = run_warning_detection_for_target(self.db, "micro_major", micro_major.id)
        self.assertEqual(len(result.created), 0)
        self.assertEqual(len(result.skipped), 1)


if __name__ == "__main__":
    unittest.main()
