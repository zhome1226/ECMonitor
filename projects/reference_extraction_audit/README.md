# Reference Extraction Audit

本项目负责 **人工基准（manual benchmark）vs 模型提取结果（model extraction）** 的长表化、覆盖率统计、差异归因和复核输出。

> 详细总说明见：[`THREE_LIBRARY_EXECUTION_WORKFLOW_20260331.md`](THREE_LIBRARY_EXECUTION_WORKFLOW_20260331.md)

---

## 1. 项目职责

本项目不是原始 PDF 获取器，也不是附表数据库重建器，而是三库工作的**审计层 / 对账层**。

它负责回答：

- 哪些记录进入了 manual-vs-model 比较？
- exact 1:1 到底怎么定义？
- `<MDL` / `ND` / `NA` 应该被算成什么？
- 差异到底来自缺 PDF、缺 source、单位问题、非数值问题，还是模型抽取错误？
- 哪些产出可以直接用来做 1:1 agreement plot 或 figure reconstruction？

---

## 2. 本轮三库统一统计（当前口径）

| 文献库 | 一开始文献数 | 一开始记录数 | 成功获取文献数 | 成功获取记录数 |
|---|---:|---:|---:|---:|
| Science | 102 | 1894 | 95 | 1733 |
| Nature | 240 | 324 | 239 | 323 |
| EST | 26 | 88 | 26 | 88 |

合计：

- 初始：`368` 篇文献 / `2306` 条记录
- 成功获取：`360` 篇文献 / `2144` 条记录

> 说明：这些数字采用的是当前 benchmark-comparable 口径，而不是所有历史 PDF 抽取产出的总行数。

---

## 3. 核心脚本

- `build_reference_extraction_audit.py`
- `build_nature_science_manual_vs_codex.py`

---

## 4. 主要输入

### Manual benchmark

- `four_studies_reference_database_with_doi.xlsx`
- `sciadv_2024_逐篇文献提取数值整理.xlsx`
- `parsed_table_s1.csv` / `tfa_literature_database_rebuilt.xlsx`

### Model outputs

- Nature extracted JSON / records
- Science seawater records CSV
- EST rebuilt workbook / entry-level reextract files

### Mapping / audit support

- DOI mapping
- PDF manifest
- failure reason notes
- source audit tables

---

## 5. Science 在本项目中的关键成果

### 5.1 逐条 paired long table

关键文件：

- `/home/zhome/science_paired_long_all102.csv`
- `/home/zhome/science_paired_long_available95.xlsx`
- `/home/zhome/science_recheck_summary.xlsx`
- `/home/zhome/science_longtable_build_report.md`

### 5.2 当前规模

- all102 long rows：`45,456`
- available95 long rows：`41,592`
- all102 unique paper-row ids：`1,894`
- available95 unique paper-row ids：`1,733`

### 5.3 这一步最重要的规则修正

Science 的 `exact_1_to_1` 这次明确采用：

> 只有 **标准化后的 numeric 值完全一致** 才记为 `exact_1_to_1 = 1`

因此：

- `<MDL`
- `ND`
- `LOD`
- 其他不可解析 numeric 的文本

都不会再被当作 numeric exact。

这也是为什么新长表与旧汇总表在多个参数上一致率明显下降：
不是模型忽然变差，而是定义变得严格、可解释、可复现。

---

## 6. Nature / EST 在本项目中的关键成果

### 6.1 like-science 长表

关键文件：

- `/home/zhome/nature_paired_long_pollutant_fixed.csv`
- `/home/zhome/est_paired_long_pollutant_fixed.csv`
- `/home/zhome/nature_est_paired_long_pollutant_fixed.xlsx`
- `/home/zhome/nature_est_pollutant_fix_note.md`

### 6.2 本轮修复的关键语义问题

之前 Nature / EST 的 `pollutant` 列曾经错误地放入：

- `min_conc`
- `max_conc`
- `avg_conc_ugL`
- `pct_gt10`

这其实是统计量名，不是真污染物名。

本轮修复后：

- Nature 当前 benchmark 语义为：`sum_PFAS`
- EST 当前 benchmark 语义为：`TFA`
- 统计量单独进入 `statistic_type`

---

## 7. 这个项目输出什么类型的结论

本项目不只输出“一个百分比”，而是输出：

1. 覆盖率
2. exact / within 2x / within 3x
3. missing / both missing / one missing
4. nonnumeric
5. raw text matches but not numeric
6. failure reason 分类
7. paired long table
8. summary workbook
9. difference-source notes

这使得后续分析可以真正追溯：
- 为什么某参数看起来准；
- 为什么某参数看起来不准；
- 到底是模型问题，还是口径 / source 问题。

---

## 8. 当前项目要解决的核心问题

这个项目在三库闭环中解决的是：

> “人工值和模型值到底在哪一层一致 / 不一致，差异来自哪里？”

因此它是最终用于：

- 做逐条对账
- 做 1:1 agreement plot 前整理
- 做 figure reconstruction 前整理
- 做后续误差分析与方法学说明

的关键项目。
