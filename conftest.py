"""让 pytest 也能直接运行。

包采用扁平布局（``pdftext/`` 就在仓库根目录），所以从仓库根目录执行时
``import pdftext`` 本来就可用；这里额外把 ``tests/`` 加进路径，
好让测试文件能直接 ``import pdfbuilder``。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent

for path in (ROOT, ROOT / "tests"):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)
