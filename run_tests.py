#!/usr/bin/env python
"""零依赖测试入口。

本项目的卖点是"不需要安装任何东西"，所以测试同样只用标准库——
不需要 pytest，clone 下来直接跑::

    python run_tests.py

（若你习惯 pytest，仓库根目录也有 conftest.py，``pytest`` 同样可用。）
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
TESTS = ROOT / "tests"

# 扁平布局：包在仓库根目录，把根目录与 tests 放进导入路径
for path in (str(ROOT), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS), pattern="test_*.py", top_level_dir=str(TESTS))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
