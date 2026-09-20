# pdftext

> **零依赖的纯 Python PDF 文本提取器。** 只用标准库解析 PDF 并抽出文字与坐标。

[![CI](https://github.com/Ray144165154/pdftext/actions/workflows/ci.yml/badge.svg)](https://github.com/Ray144165154/pdftext/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Dependencies](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen)](#零依赖不是口号)

```bash
python -m pdftext 文档.pdf
```

不需要 `pip install`，不需要虚拟环境，不需要任何东西。**下载下来就能跑。**

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [30 秒上手](#30-秒上手)
- [支持的特性](#支持的特性)
- [命令行用法](#命令行用法)
- [Python API](#python-api)
- [它是怎么工作的](#它是怎么工作的)
- [零依赖不是口号](#零依赖不是口号)
- [测试](#测试)
- [与其他工具对比](#与其他工具对比)
- [已知限制](docs/LIMITATIONS.md)
- [项目结构](#项目结构)
- [许可](#许可)

---

## 它解决什么问题

绝大多数 PDF 库都要装一堆东西：`pdfminer.six` 拉进 `charset-normalizer`、`cryptography`；
`PyMuPDF` 是个几十兆的 C 扩展；`pdfplumber` 底下又是一整套 `pdfminer`。

有时候你只想**在一个干净的环境里，把手头那个 PDF 的文字读出来**——比如
一次性脚本、教学演示、受限的服务器、或者你就是不想为了读个文件装 200MB 的依赖。

`pdftext` 就是干这个的：**2746 行、纯 Python、零依赖**，实现了 PDF 文本提取真正需要的那部分规范。

它的设计目标按优先级排序：

1. **零运行时依赖** —— 只有标准库，且这一点由 CI 自动验证
2. **代码可读** —— 每个模块解决一个明确问题，关键处写清"为什么这么做"
3. **面向中文 PDF** —— CID 字体 + ToUnicode CMap 是重点照顾对象，不是顺带支持

它不是性能冠军，也不打算成为全能 PDF 工具箱。定位见[与其他工具对比](#与其他工具对比)。

---

## 30 秒上手

```bash
git clone https://github.com/Ray144165154/pdftext.git
cd pdftext

# 直接跑，不用安装
python -m pdftext 你的文件.pdf
```

想装成全局命令行工具（可选）：

```bash
pip install -e .
pdftext 你的文件.pdf
```

---

## 支持的特性

### 文档结构

| 特性 | 状态 |
|---|---|
| 传统交叉引用表（`xref` + `trailer`） | ✅ |
| 交叉引用流 `/Type /XRef`（PDF 1.5+） | ✅ |
| 对象流 `/Type /ObjStm`（对象压缩存储） | ✅ |
| 增量更新链 `/Prev`、混合引用 `/XRefStm` | ✅ |
| **交叉引用损坏时自动扫描重建** | ✅ |
| 页面树 `/Kids` 递归 + 可继承属性（`/Resources` `/MediaBox` `/Rotate`） | ✅ |
| `/Rotate` 0/90/180/270 与 `MediaBox` 偏移归一化 | ✅ |
| 加密 PDF（`/Encrypt`） | ❌ 明确报错，不静默出错 |

### 流解码

`FlateDecode`（含 PNG 与 TIFF 预测器）、`LZWDecode`、`ASCIIHexDecode`、
`ASCII85Decode`、`RunLengthDecode`。图像编码（`DCTDecode` 等）原样跳过。

### 字体与编码

| 特性 | 状态 |
|---|---|
| 简单字体（Type1 / TrueType / Type3）+ `/Widths` | ✅ |
| `/Encoding` 具名编码（WinAnsi / MacRoman / Standard / PDFDoc） | ✅ |
| `/Encoding` 字典 + `/Differences` 字形名 | ✅ |
| 复合字体 Type0 / `Identity-H` + `/W` `/DW` 宽度 | ✅ |
| `/ToUnicode` 的 `bfchar` | ✅ |
| `/ToUnicode` 的 `bfrange` 递增写法 | ✅ |
| **`/ToUnicode` 的 `bfrange` 数组写法** `<lo> <hi> [<d1> <d2>…]` | ✅ |
| `codespacerange` 自动判定单/双字节编码 | ✅ |
| 代理对（补充平面字符）与多字符映射（连字） | ✅ |
| 竖排 `Identity-V` | ⚠️ 能取到字，但按横排处理 |
| 字形名算法命名 `uniXXXX` / `uXXXXX` | ✅ |

### 内容流解释

`q` `Q` `cm` `BT` `ET` `Tf` `Td` `TD` `Tm` `T*` `TL` `Tc` `Tw` `Tz` `Ts` `Tr`
`Tj` `TJ` `'` `"` `Do`（Form XObject，含递归与 `/Matrix`）、内联图像 `BI…EI` 跳过。

### 版面还原

- 按基线 y 聚类成行，阈值**相对字号**而非固定点数
- 空格判定基于**上一字的实际前进量**（已含 `Tc` / `TJ` 调整 / `Tz`），
  而不是固定间距阈值
- 自动去重"伪粗体"的重复绘制
- `--layout` 模式按 x 坐标近似还原列对齐（适合作课表、财务报表）

---

## 命令行用法

```bash
# 基本提取，输出到标准输出
python -m pdftext 文档.pdf

# 只取第 1-3 页和第 7 页
python -m pdftext 文档.pdf -p 1-3,7

# 保留近似版面（表格类 PDF 很有用）
python -m pdftext 表格.pdf --layout

# 输出 JSON（含每页尺寸、旋转角、每行坐标）
python -m pdftext 文档.pdf --json

# 只看统计信息
python -m pdftext 文档.pdf --stats

# 写入文件
python -m pdftext 文档.pdf -o 输出.txt

# 一次处理多个文件
python -m pdftext a.pdf b.pdf -o 合并.txt
```

`--stats` 会顺便告诉你这份 PDF **是不是扫描件**（没有文本层时给出明确提示，
而不是默默输出空字符串）：

```
文件: 扫描件.pdf
PDF 版本: 1.4
对象数: 42
总页数: 3
已处理: 3
字符数: 0
含文本层的页: 0/3
提示: 没有任何文本层，这多半是扫描件（纯图片），需要 OCR 才能取字。
```

---

## Python API

```python
from pdftext import extract_text, extract_pages, PdfDocument, Extractor

# 最简单：拿到整篇文本
print(extract_text("文档.pdf"))

# 按页处理
for page in extract_pages("文档.pdf", pages="1-3"):
    print(f"第 {page.number} 页  {page.width}×{page.height}pt  旋转 {page.rotation}°")
    for line in page.lines:
        print(f"  y={line.y:7.2f} x={line.x:7.2f} size={line.size:5.2f}  {line.text}")
```

需要更细的控制时，直接用底层对象：

```python
from pdftext import PdfDocument, Extractor

doc = PdfDocument.from_file("文档.pdf")

print(doc.version)        # '1.7'
print(doc.page_count)     # 12
print(doc.repaired)       # True 表示交叉引用损坏、走了扫描重建
print(doc.root)           # 文档目录字典

extractor = Extractor(doc)
page = extractor.extract_page(0)      # 页码从 0 开始

# 带坐标的字符级数据
for char in page.chars[:5]:
    print(char.text, char.x, char.y, char.size, char.color)

# 等宽近似版面
print(page.layout_text())
```

也可以直接从内存里的字节解析（比如从网络下载、从压缩包取出）：

```python
from pdftext import PdfDocument

doc = PdfDocument.from_bytes(raw_bytes)
```

---

## 它是怎么工作的

```
             ┌──────────────────────────────────────────────────┐
   字节流 →  │ objects.py    词法分析 / 对象解析                 │
             │               共享一个 Lexer，解析器与内容流复用   │
             └───────────────────────┬──────────────────────────┘
                                     ↓
             ┌──────────────────────────────────────────────────┐
             │ document.py   交叉引用 / 对象流 / 页面树 / 容错重建 │
             └───────────────────────┬──────────────────────────┘
                                     ↓
             ┌──────────────────────────────────────────────────┐
             │ filters.py    解码链（Flate/LZW/A85/… + 预测器）    │
             └───────────────────────┬──────────────────────────┘
                                     ↓
      ┌──────────────────────────────┴───────────────────────────┐
      │ fonts.py  字体：编码表 / 字形宽度 / ToUnicode 映射          │
      │ cmap.py   ToUnicode CMap 解析                             │
      └──────────────────────────────┬───────────────────────────┘
                                     ↓
             ┌──────────────────────────────────────────────────┐
             │ content.py    内容流状态机 → 带设备坐标的字符序列    │
             └───────────────────────┬──────────────────────────┘
                                     ↓
             ┌──────────────────────────────────────────────────┐
             │ layout.py     行聚类 / 空格判定 → 文本             │
             └───────────────────────┬──────────────────────────┘
                                     ↓
             ┌──────────────────────────────────────────────────┐
             │ extract.py / cli.py   按页组织 / 输出格式          │
             └──────────────────────────────────────────────────┘
```

PDF 文本提取看起来像"把文字读出来"，实际上要同时处理四类互相纠缠的问题。
下面这几个点，每一个写错都会让整页输出变成乱码——它们也正是这类程序最容易出错的地方。

### 1. 图形状态栈必须真的恢复

内容流里的 `q` 保存图形状态、`Q` 恢复。排版软件普遍这么写：

```
q  1 0 0 1 0 -100 cm   BT ... Tj ET   Q
q  1 0 0 1 0 -200 cm   BT ... Tj ET   Q
```

如果 `Q` 没有真的恢复 CTM，变换就会不断复合：第一块偏 100、第二块偏 300、
第三块偏 600……**整页坐标迅速塌缩成一团**，不同行被合并，字符互相交错。

真实症状长这样（同一页的 8 次变换全部叠在一起）：

```
y=590.0   2-2学--22年34校历          ← 期望 y 从 480 一路排到 124
y=585.0   十十十十十十十十十十十十
y=583.6   一7122511241289122396712263401124118812118815225521125239112239671202745111241289122396630
```

`tests/test_regressions.py::TestGraphicsStateRestore` 就是针对它的回归测试。

### 2. 文本矩阵要按字形宽度推进

`Tj` 显示的字符串里每个字都有宽度，必须查字体的 `/Widths`（简单字体）
或 `/W`（CID 字体），按 `w0/1000 × 字号` 推进文本矩阵。

**不推进宽度，同一个 `Tj` 里的所有字符就会堆在同一个坐标上**，
输出只剩最后一个字，或者顺序完全错乱。

### 3. 字距调整与"真的是空格"要能区分

`TJ` 数组里夹的数字是字距调整（单位 1/1000 文本空间），不是空格：

```
[(He) -120 (llo) 60 ( World)] TJ
```

反过来，判断"两个字符之间要不要输出空格"也不能用固定点数阈值。
用 `x - prev_x > 6` 这种写法，在 6pt 左右的小字号中文上会**每个字都插一个空格**：

```
本 科 老 生 报 到          ← 错误
本科生报到前请              ← 正确
```

正确做法是拿**前一个字符的实际前进量**算出"下一个字本来该在哪"，
再看实际位置偏了多少——前进量里已经包含了 `Tc` / `TJ` / `Tz` 的全部影响。

### 4. 每个字体的 CMap 是独立的

CID 字体在内容流里写的是字形编号，要靠 `/ToUnicode` 还原文字。
**两个子集字体的码位空间互不相干**：码 `1` 在字体 A 里是"甲"，在字体 B 里可能是"乙"。

把所有字体的 CMap 合并成一张全局表，必然有一半解错：

```python
cm = parse_cmap(font_a)
cm.update(parse_cmap(font_b))   # ✗ 后一个覆盖前一个，A 的字全错了
```

必须**按字体分别解析、分别查表**。

### 5. 现代 PDF 的对象不在文件里

PDF 1.5 起，对象可以被压进"对象流"（`/Type /ObjStm`）。这些对象在文件里
**没有自己的 `N G obj` 实体**，只能通过交叉引用流的 type-2 条目找到。

用正则扫 `N G obj` 的解析器会认为这些对象"不存在"，于是提不出任何内容。

---

## 零依赖不是口号

项目里有一个会**在 CI 每个平台、每个 Python 版本上运行**的检查脚本：

```bash
$ python tools/check_zero_deps.py
已检查 11 个源文件，运行时导入全部来自标准库 ✔
```

它会静态解析 `src/pdftext` 下所有文件的 import，任何一个第三方模块都会让构建失败。
这样"零依赖"就不会因为某天顺手加个 `import requests` 而悄悄失效。

（测试也只用标准库 `unittest`——`pip install pytest` 不是运行测试的前提。）

---

## 测试

```bash
python run_tests.py
```

```
Ran 156 tests in 0.082s

OK
```

156 个测试，覆盖词法/语法、解码器、CMap、内容流状态机、版面还原、
文档结构与容错、CLI，以及一组针对具体历史缺陷的回归测试。

**测试夹具是纯 Python 生成的**（`tests/pdfbuilder.py`）——仓库里不放二进制 PDF 样本。
这样每个用例想测什么都一清二楚，可以精确构造
"故意把 `bfrange` 写成数组形式"、"故意让 `Q` 不恢复"这类边界情况，
也避免了样本文件的版权与体积问题。

---

## 与其他工具对比

| | pdftext | pdfminer.six | PyMuPDF | pypdf |
|---|---|---|---|---|
| 运行时依赖 | **0** | 多个 | C 扩展（数十 MB） | 多个 |
| 纯 Python | ✅ | ✅ | ❌ | ✅ |
| 中文 CID + ToUnicode | ✅ | ✅ | ✅ | ⚠️ 部分 |
| 交叉引用损坏自动重建 | ✅ | ⚠️ 有限 | ✅ | ⚠️ 有限 |
| 提取速度（单页） | ~3.5 ms | 较慢 | **最快** | 中等 |
| 代码规模 | 2746 行 | 数万行 | 数万行（含 C） | 数万行 |
| 适合 | 零依赖场景、教学、读源码 | 通用提取 | 高性能/渲染/编辑 | PDF 操作 |

**说白了：**

- 你要**性能和全能**（渲染、OCR、编辑、表单）→ 用 **PyMuPDF**
- 你要**成熟的版面分析与表格提取** → 用 **pdfplumber / pdfminer.six**
- 你要**拆分合并改 PDF** → 用 **pypdf**
- 你要**零依赖、读得懂源码、能塞进任何环境** → 用 **pdftext**

`pdftext` 不试图取代上面任何一个。

---

## 项目结构

```
pdftext/
├── pdftext/               包本体（扁平布局，clone 下来即可直接运行）
│   ├── objects.py     词法分析器 + 对象解析器（共享给内容流）
│   ├── filters.py     Flate / LZW / ASCII85 / ASCIIHex / RunLength + 预测器
│   ├── document.py    交叉引用、对象流、页面树、容错重建
│   ├── cmap.py        ToUnicode CMap（三种写法全覆盖）
│   ├── fonts.py       字体编码、字形宽度、字形名 → Unicode
│   ├── content.py     内容流状态机（图形状态 + 文本矩阵）
│   ├── layout.py      行聚类与空格判定
│   ├── extract.py     顶层提取 API
│   └── cli.py         命令行接口
├── tests/             156 个测试（含纯 Python 的 PDF 生成器）
├── tools/             零依赖检查脚本
├── docs/              限制说明
├── run_tests.py       零依赖测试入口
└── pyproject.toml
```

> **关于布局**：这里刻意用扁平布局而不是 `src/` 布局。`src/` 布局在打包上更严谨，
> 但它要求先 `pip install` 才能 `python -m pdftext`——而"clone 下来直接跑"
> 正是本项目的核心卖点。取舍之后选了扁平。

---

## 贡献

欢迎提 Issue 和 PR。如果你遇到提取错误的 PDF：

1. 先跑 `python -m pdftext 你的文件.pdf --stats` 确认**是否有文本层**
2. 用 `--json` 看每行的坐标是否正确
3. 提 Issue 时请附上：`--stats` 输出、期望的文字、实际得到的文字

**请不要直接上传受版权保护的 PDF**——用 `tests/pdfbuilder.py` 造一个最小的
复现样本更有价值，也更便于定位问题。

---

## 许可

[MIT](LICENSE)
