# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

## [0.1.0] - 2026-09-14

首个版本。实现了零依赖的 PDF 文本提取，覆盖实际 PDF 里最常见的结构。

### 新增

**文档结构**
- 传统交叉引用表（`xref` + `trailer`）解析
- 交叉引用流 `/Type /XRef`（PDF 1.5+），支持 `/W` `/Index`
- 对象流 `/Type /ObjStm`，支持 type-2 交叉引用条目
- 增量更新链 `/Prev` 与混合引用 `/XRefStm`
- 交叉引用损坏时的全文件扫描重建（`doc.repaired` 标记）
- 页面树递归遍历、环检测，以及 `/Resources` `/MediaBox` `/CropBox` `/Rotate` 继承
- `/Rotate` 0/90/180/270 与 `MediaBox` 非零原点归一化
- 加密 PDF 检测（明确报错，不静默给出乱码）

**流解码**
- `FlateDecode`（含截断/裸 deflate 的降级重试）
- `LZWDecode`（含 `EarlyChange`）
- `ASCIIHexDecode`、`ASCII85Decode`、`RunLengthDecode`
- PNG 预测器（None/Sub/Up/Average/Paeth）与 TIFF 预测器

**字体与编码**
- 简单字体（Type1 / TrueType / Type3）与 `/Widths` / `/MissingWidth`
- 具名编码：WinAnsi、MacRoman、Standard、PDFDoc、MacExpert
- `/Encoding` 字典与 `/Differences`，字形名转 Unicode（含 `uniXXXX` / `uXXXXX`）
- 复合字体 Type0 / `Identity-H`，`/W` 三种写法的宽度解析与 `/DW`
- `/ToUnicode`：`bfchar`、`bfrange` 递增写法、**`bfrange` 数组写法**
- `codespacerange` 自动判定源编码字节宽度
- UTF-16BE 代理对合并与多字符（连字）映射

**内容流**
- 图形状态：`q` `Q` `cm`
- 文本对象与定位：`BT` `ET` `Td` `TD` `Tm` `T*`
- 文本状态：`Tf` `TL` `Tc` `Tw` `Tz` `Ts` `Tr`
- 显示文字：`Tj` `TJ` `'` `"`
- 按字形宽度推进文本矩阵
- Form XObject（`Do`）递归展开，支持 `/Matrix`
- 内联图像 `BI…EI` 跳过（避免被二进制体里的 `EI` 带偏）
- 颜色记录：`g` `rg` `k`

**版面还原**
- 按基线聚类成行，阈值相对字号而非固定点数
- 基于实际前进量（含 `Tc` / `TJ` / `Tz`）的空格判定
- 伪粗体重绘去重
- `--layout` 等宽近似版面模式

**接口**
- 命令行：`-p/--pages`、`-o/--output`、`-l/--layout`、`-j/--json`、`-s/--stats`、`-q/--quiet`
- 支持多文件、单文件失败不中断整批
- Python API：`extract_text`、`extract_pages`、`PdfDocument`、`Extractor`
- 可从路径或内存字节解析

**工程**
- 152 个测试，仅用标准库 `unittest`
- 纯 Python 的 PDF 生成器作为测试夹具（仓库内不放二进制样本）
- `tools/check_zero_deps.py`：静态验证运行时零依赖，CI 中强制执行
- GitHub Actions：Ubuntu / Windows / macOS × Python 3.10–3.13

### 说明

- 运行时依赖数量：**0**
- 源码 2699 行，测试 1225 行

[Unreleased]: https://github.com/Ray144165154/pdftext/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Ray144165154/pdftext/releases/tag/v0.1.0
