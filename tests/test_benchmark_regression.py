"""benchmarks.py 的 smoke 测试。

验证 benchmarks 模块能正常运行，且回归检测逻辑正确工作。
不验证具体性能数值（受运行环境影响），仅验证流程可用。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 确保 src/ 在 import 路径中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
# benchmarks.py 在项目根目录
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def benchmarks_mod():
    """导入 benchmarks 模块（项目根目录）。"""
    import benchmarks

    return benchmarks


def test_benchmarks_run_all(benchmarks_mod) -> None:
    """run_all_benchmarks 应返回非空 dict，且所有值为正浮点数。"""
    results = benchmarks_mod.run_all_benchmarks()
    assert isinstance(results, dict)
    assert len(results) >= 8  # 至少 8 个基准
    for label, ms in results.items():
        assert isinstance(label, str)
        assert isinstance(ms, float)
        assert ms > 0, f"{label} 的 ms/op 应为正数"


def test_benchmarks_baseline_dict_exists(benchmarks_mod) -> None:
    """内置 BASELINE 字典应包含所有基准的基线值。"""
    baseline = benchmarks_mod.BASELINE
    assert isinstance(baseline, dict)
    assert len(baseline) >= 8
    for label, ms in baseline.items():
        assert isinstance(label, str)
        assert isinstance(ms, (int, float))
        assert ms > 0


def test_check_regression_no_regressions(benchmarks_mod) -> None:
    """check_regression 在当前值低于阈值时应返回空列表。"""
    baseline = {"test_op": 1.0}
    current = {"test_op": 1.1}  # 10% 提升，未超 20% 阈值
    regressions = benchmarks_mod.check_regression(current, baseline, threshold=1.2)
    assert regressions == []


def test_check_regression_detects_regression(benchmarks_mod) -> None:
    """check_regression 在当前值超过阈值时应返回退化项。"""
    baseline = {"test_op": 1.0}
    current = {"test_op": 1.5}  # 50% 退化，超过 20% 阈值
    regressions = benchmarks_mod.check_regression(current, baseline, threshold=1.2)
    assert len(regressions) == 1
    assert "test_op" in regressions[0]


def test_check_regression_missing_label_skipped(benchmarks_mod) -> None:
    """基线中有但当前结果中没有的标签应被跳过，不报退化。"""
    baseline = {"test_op": 1.0, "missing_op": 2.0}
    current = {"test_op": 1.0}  # missing_op 不在 current 中
    regressions = benchmarks_mod.check_regression(current, baseline, threshold=1.2)
    assert regressions == []


def test_save_and_load_baseline(benchmarks_mod, tmp_path: Path) -> None:
    """save_baseline + load_baseline 应能往返保存和加载。"""
    results = {"test_op": 1.234, "another_op": 5.678}
    path = str(tmp_path / "baseline.json")
    benchmarks_mod.save_baseline(results, path)
    loaded = benchmarks_mod.load_baseline(path)
    assert loaded == results
