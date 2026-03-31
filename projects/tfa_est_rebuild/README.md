# EST / TFA Literature Rebuild

本项目负责把 EST 相关 TFA 文献从附表 / 引文 / PDF / DOI 线索中，重建成可计算、可审计、可用于 manual-vs-model 对账的数据库。

> 详细总说明见：[`../reference_extraction_audit/THREE_LIBRARY_EXECUTION_WORKFLOW_20260331.md`](../reference_extraction_audit/THREE_LIBRARY_EXECUTION_WORKFLOW_20260331.md)

---

## 1. 项目职责

这个项目不是 PDF 批量抽取器，而是 **文献数据库重建器**。

它的核心职责是：

1. 从原始附表 / 中间清洗文件中恢复 TFA 监测记录；
2. 为每条 reference short 找到可靠 DOI；
3. 把 DOI、标题、全文、Zotero、本地 PDF 对齐起来；
4. 只保留后续 benchmark 对账真正需要的 water media 子集；
5. 生成可用于 audit、聚合、图复现的基线数据库。

---

## 2. 本轮工作中的统计口径

### 更宽的原始层

来自 `parsed_table_s1.csv`：

- 原始监测记录：`169`
- 原始参考文献短引：`43`

### 当前 benchmark 比较层

在本轮统一统计中，只保留：

- water media
- matched DOI
- 与 manual / model 对账一致的记录结构

因此当前口径是：

- 初始文献：`26`
- 初始记录：`88`
- 成功获取文献：`26`
- 成功获取记录：`88`

> 即 EST 在当前对账子集里，water + matched DOI 的记录已经全部进入可比较状态。

---

## 3. 输入文件

### 核心输入

- `parsed_table_s1.csv`
- `reference_resolution_audit.csv`
- `docx_title_doi_url_mapping.csv`
- `reference_list_from_main_pdf.csv`
- 本地下载 PDF 与 Zotero PDF

### 关键脚本

- `build_tfa_database.py`

---

## 4. 重建流程

### Step 1：解析原始监测记录

恢复字段包括：

- `source_reference_short`
- `chemical_name`
- `medium`
- `subcategory`
- `location`
- `country_or_region`
- `sampling_year_or_period`
- `min_concentration`
- `max_concentration`
- `mean_concentration`
- `median_concentration`
- `concentration_unit`

### Step 2：建立 reference → DOI 映射

综合利用：

- 主文 PDF 的参考文献
- docx 标题 / DOI / URL 对照
- 下载文件名
- Zotero 命中结果

形成：

- `matched_doi`
- `matched_full_reference`
- `resolution_method`
- `local_pdf_found`
- `zotero_match_path`

### Step 3：生成 DOI 审计轨迹

所有 DOI 匹配都不会“默默替换”，而是落到：

- `reference_resolution_audit.csv`

这样后续可以复查：
- 这篇 DOI 是从哪里来的；
- 是 docx 命中、PDF reference 命中，还是 Zotero 命中；
- 哪些文献仍然 unresolved。

### Step 4：做 benchmark 子集过滤

当前用于比较的 water media 包括：

- `precipitation`
- `surface water`
- `drinking water`
- `waste water`
- `groundwater`
- `ice`
- `landfill leachate`

不把 `plants`、`soil`、`dust`、`air`、`human serum` 等非当前目标介质混入 benchmark 子集。

### Step 5：写出重建数据库

主要输出：

- `tfa_literature_database_rebuilt.xlsx`
- `summary.json`

当前 `summary.json` 中保留了：

- `total_monitoring_records = 169`
- `matched_full_references = 102`
- `matched_doi_from_docx = 168`
- `grey_literature_or_report_records = 1`
- `unresolved_rows = 1`

---

## 5. 与后续细粒度提取的关系

这个项目产出的数据库是 **baseline rebuild**，并不等于最终所有 entry-level 复现。

在此基础上，本轮还做了更细的 entry-level 重提取，例如：

- `/home/zhome/est_tfa_model_reextract_v4_confidence_20260330/est_tfa_entrylevel_records_v4_expanded.csv`

当前该文件：

- 行数：`293`
- DOI 数：`11`

这代表：

- `tfa_est_rebuild` 解决“文献库能不能稳定重建”；
- 后续 entry-level 文件解决“能不能把文献里的 TFA 记录拆到更细层”。

---

## 6. 关键输出文件

- `build_tfa_database.py`
- `parsed_table_s1.csv`
- `parsed_table_s1.json`
- `reference_resolution_audit.csv`
- `tfa_literature_database_rebuilt.xlsx`
- `summary.json`

---

## 7. 当前项目结论

本项目在三库闭环中解决的是：

> “EST/TFA 的原始文献库能不能从附表和参考文献层稳定重建成可计算数据库？”

没有这个项目，后续任何 TFA benchmark 比较、entry-level 重提取、图复现都会失去统一 reference / DOI 基线。
