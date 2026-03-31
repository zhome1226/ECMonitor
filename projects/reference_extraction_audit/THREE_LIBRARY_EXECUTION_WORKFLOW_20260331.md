# 三个文献库工作展开说明（Nature / Science / EST）

> 生成时间：2026-03-31
> 目的：把本轮三个文献库相关工作的实际展开方式、统计口径、关键脚本、输入输出文件、阶段性结果和后续维护建议写成一份可直接复用的说明文档；后续可同步到 GitHub 中各项目的 README。

---

## 1. 这次三项工作的整体目标

这三项工作并不是单纯“跑一遍提取脚本”，而是围绕三个不同 benchmark / 文献库，分别完成了以下闭环：

1. **确定人工基准（manual benchmark）到底是什么**  
   不同文献库的人工基准口径并不一样：
   - Nature 是 `four_studies_reference_database_with_doi.xlsx` 中 `Nature2024_Refs` 的 PFAS 水介质汇总行；
   - Science 是 `sciadv_2024_逐篇文献提取数值整理.xlsx` 的逐文献原始值；
   - EST 是 `parsed_table_s1.csv` + `reference_resolution_audit.csv` 对应的 TFA 文献重建表。

2. **重新核对可用全文 / PDF 覆盖情况**  
   我们不直接相信旧汇总，而是回到本地 PDF、manifest、audit 表，重新定义：
   - 哪些文献一开始就在人工库里；
   - 哪些文献后来成功拿到了本地 PDF 或 `source_ready` 文本；
   - 哪些文献虽然有条目，但并没有真正进入“可比较”的提取流程。

3. **把模型提取结果整理成可比结构**  
   三个库最终都要落到“paper × parameter / pollutant”的逐条比较层，而不是只停留在 paper-level summary。

4. **在不同层级做质量控制（QC）**  
   包括：
   - DOI 归一化；
   - water-only / seawater-only 过滤；
   - 产品、沉积物、生物等非目标介质剔除；
   - 单位换算；
   - `<MDL` / `ND` / `NA` 等非数值内容单独处理；
   - 1:1、2x、3x、一致率、缺失率、不可解析率等指标重算。

5. **把结果重新组织成后续可复现的项目资产**  
   不只是生成临时 Excel，而是把工作拆到三个项目中：
   - `nature_science_extraction`：Nature / Science 的 PDF 解析、网页回退、结构化抽取；
   - `tfa_est_rebuild`：EST / TFA 的原始文献库重建、DOI 匹配、记录整理；
   - `reference_extraction_audit`：人工 vs 模型对账、长表、覆盖率、误差与差异来源审计。

---

## 2. 这次采用的统一统计口径

为了避免不同文件之间“文献数”和“记录数”口径不一致，这次总结采用了**当前可复核的统一口径**。

### 2.1 一开始有多少篇文献 / 多少条记录

这里的“**一开始**”指的是：

- **Science**：Science Advances 2024 这套 benchmark 手工工作簿中的全部目标记录；
- **Nature**：`Nature2024_Refs` 中 **有 DOI + 非 PRODUCT + 水介质** 的基准记录；
- **EST**：`parsed_table_s1.csv` 中 **water media + 已匹配 DOI** 的基准记录。

### 2.2 成功获取多少篇文献 / 多少条记录

这里的“**成功获取**”指的是：

- 本地已经有可进入提取流程的 PDF，或者在 manifest 中已经标记为 `source_ready`；
- 并且这些文献对应的 benchmark 记录可以进入后续比较；
- **不是**简单指目录里出现了某个 PDF 文件名，
- 也**不是**所有抽取器历史上曾经输出过一行 JSON 就算成功。

### 2.3 当前汇总结果（统一口径）

| 文献库 | 一开始文献数 | 一开始记录数 | 成功获取文献数 | 成功获取记录数 | 说明 |
|---|---:|---:|---:|---:|---|
| Science | 102 | 1894 | 95 | 1733 | all102 / available95 |
| Nature | 240 | 324 | 239 | 323 | `Nature2024_Refs` 中 water + non-PRODUCT + DOI |
| EST | 26 | 88 | 26 | 88 | `parsed_table_s1.csv` 中 water media + matched DOI |

**合计：**
- 一开始：**368 篇文献，2306 条记录**
- 成功获取：**360 篇文献，2144 条记录**

### 2.4 两个非常重要的补充说明

#### Nature 还有一个更宽的“PDF 批量抽取”口径
Nature 这边有一个更宽的 PDF 级工作流，其 manifest 是：

- `/home/zhome/reextract_runs/study_metric_reextract_20260327_refresh/Nature/manifest.json`

这个层面上：
- manifest 总 paper 数：**327**
- 其中 `source_ready`：**276**
- `no_source`：**51**

但这 **327** 不是当前你让我汇总的 benchmark 口径；它是 PDF 批量提取工作流的总 paper 数。

#### EST 也有一个更宽的原始监测表口径
EST 原始表：

- `/mnt/c/Users/Administrator/.codex/worktrees/15ad/codex/projects/tfa_est_rebuild/parsed_table_s1.csv`

更宽的原始层面是：
- 原始监测记录：**169**
- 原始参考文献短引：**43**

但进入当前“可直接和 benchmark 对账”的 water + DOI 子集后，采用的是：
- **26 篇文献 / 88 条记录**

---

## 3. 三项工作的展开方式：总体分层

这三项工作实际上是按 **四层结构** 展开的：

### 第一层：原始 benchmark 层
关注“人工基准到底长什么样”。

- Nature：`four_studies_reference_database_with_doi.xlsx`
- Science：`sciadv_2024_逐篇文献提取数值整理.xlsx`
- EST：`parsed_table_s1.csv`、`reference_resolution_audit.csv`

### 第二层：全文 / PDF 解析层
关注“哪些文献有完整 source，哪些只有一页，哪些要网页回退”。

- `nature_science_extraction/workflow.py`
- Nature / Science 的 run manifest
- EST 的本地 PDF 审计表

### 第三层：结构化提取层
关注“模型到底从原文中抽了什么”。

- Nature：记录级环境浓度抽取
- Science：逐 paper × parameter 的 seawater 结构化值
- EST：TFA entry-level / aggregated 重建表

### 第四层：对账与长表层
关注“manual 和 model 是否一致，以及差在哪”。

- Science 1:1 长表 / recheck summary
- Nature / EST like-science 长表
- reference audit 工作簿和差异来源说明

---

## 4. 项目一：`nature_science_extraction` 是怎么展开的

项目路径：

- `/mnt/c/Users/Administrator/codex/projects/nature_science_extraction`

核心脚本：

- `workflow.py`
- `profiles.py`
- `run_nature_science_batch.sh`

### 4.1 这个项目的职责

这个项目承担的是 **Nature / Science 两个文献库的“全文获取 + 源文本准备 + 结构化抽取入口”**。

它不是最终的人工对账层，也不是最后的图表层，而是最关键的“中游工程层”：

1. 从 DOI / 标题 / row_id 出发找到文献；
2. 优先使用本地 PDF；
3. 用 `pdfinfo` / `pdftotext` 检查 PDF 是否完整；
4. 如果本地 PDF 缺失或像只有首页，就做 DOI / Scholar / Google / 浏览器回退；
5. 生成抽取前的 source text snapshot；
6. 再把 source text 送入 Codex / 模型抽取；
7. 输出 paper-level JSON、merged JSON、manifest、run summary。

### 4.2 Nature / Science 的实际问题不是同一种

#### Nature
Nature 的主要问题不是只有“有没有 PDF”，而是：

- PDF 在，但很多文章抽取出来混入了 sediment、biota、product 等非目标介质；
- benchmark 是汇总型（例如 total PFAS 的 min/max），而模型侧是记录级、多污染物、多介质；
- 需要回到 workbook 做 water-only / non-PRODUCT 的重新对齐。

#### Science
Science 的主要问题是：

- benchmark 非常明确，目标是 `Table S2` 对齐；
- 但有些 PDF 是一页或不完整；
- 有些参数的 raw cell 是 `<MDL`、`ND` 之类，不能被简单当作 numeric exact match；
- 后续还牵涉 Figure 1 的 sampling year 重建，而不是只比较数值。

### 4.3 这一步具体怎么做

#### Step A：先从 benchmark 定义文献集合
- 读取 workbook / DOI 表
- 确定 source block
- 生成 paper input

#### Step B：优先用本地 PDF
- 搜索本地目录中的 PDF 候选
- 用 `pdfinfo` 检查页数
- 用 `pdftotext` / 页面正文关键词判断是不是完整正文
- 如果像“只有首页 / 只有摘要 / 只有 metadata”，则标记为不可直接使用

#### Step C：不完整时启动 web fallback
- DOI landing page
- Scholar / Google 搜索
- 浏览器人工介入
- 保留 source snapshot，避免后续无法复现

#### Step D：按 profile 抽取结构化结果
- Nature 用环境浓度记录型 schema
- Science 用 monitoring summary / target parameter schema

#### Step E：把结果写入 run outputs
- 每篇一个 JSON
- merged JSON / merged CSV
- manifest
- run summary

### 4.4 这个项目当前与本次任务直接相关的关键统计

#### Nature（较宽的 PDF 工作流口径）
- manifest paper 数：**327**
- `source_ready`：**276**
- `no_source`：**51**

#### Nature（当前 benchmark 比较口径）
- `Nature2024_Refs` 原始行：**504**
- 有 DOI：**396**
- 非 PRODUCT 且 water：**324 行 / 240 DOI**
- 其中成功 `source_ready` 的 benchmark DOI：**239**

#### Science（当前 benchmark 比较口径）
- 总文献：**102**
- 有 PDF：**95**
- 原始记录总数：**1894**
- available95 记录数：**1733**

### 4.5 这个项目的关键产出文件

#### Nature / Science workflow 侧
- `runs/<run_id>/...`
- manifest / summary / snapshots

#### Nature 记录级抽取侧
- `/home/zhome/nature_env_conc_extract/out_full_20260315_live/records_live.xlsx`
- 当前 live records 行数：**1144**

#### Science 后续衔接侧
- `/home/zhome/sciadv_2024_library/batch_outputs/sciadv_2024_seawater_records_available_pdfs.csv`
- 行数：**1733**

### 4.6 这个项目在本轮工作的定位

如果没有 `nature_science_extraction` 这层，后面的 Nature / Science 对账都不成立，因为：

- 你无法确认哪些文献真的拿到了可用全文；
- 也无法区分“文献缺失”和“文献存在但 PDF 只有一页”；
- 更无法把后续的差异归因到 source 缺失、source 不完整、抽取失败还是口径不一致。

---

## 5. 项目二：`tfa_est_rebuild` 是怎么展开的

项目路径：

- `/mnt/c/Users/Administrator/codex/projects/tfa_est_rebuild`

核心脚本：

- `build_tfa_database.py`

核心输入：

- `parsed_table_s1.csv`
- `reference_resolution_audit.csv`
- 本地下载 PDF / Zotero PDF / docx 标题-DOI 映射

### 5.1 这个项目的职责

这个项目负责把 EST 这篇 TFA 研究的文献库从“论文附表 / 文本 / 引文列表”重建成一个真正可计算、可追踪、可审计的数据库。

与 Nature / Science 相比，它更像是一个 **数据库重建工程**，而不是直接的 PDF 批量抽取工程。

### 5.2 这项工作的难点

EST/TFA 这套数据的难点主要有四个：

1. **原始来源是附表型、而不是直接给好的 DOI 数据库**；
2. **同一篇文献可能对应多个 medium / subcategory / location / year**；
3. **有些参考文献没有 DOI，需要靠 docx / PDF reference / Zotero 反向补 DOI**；
4. **做 benchmark 对账时，只能保留 water media 子集，不能把 plants / soil / air / dust 等一起混进去。**

### 5.3 这一步具体怎么做

#### Step A：把 TFA 原始监测表解析出来
从附表或中间清洗文件得到：
- `source_reference_short`
- `chemical_name`
- `medium`
- `subcategory`
- `location`
- `sampling_year_or_period`
- `min / max / mean / median`
- `unit`

#### Step B：建立 reference → DOI 映射
依次利用：
- 主文 PDF reference list
- docx 标题 / DOI / URL 映射
- 本地 PDF 文件名
- Zotero 匹配结果

生成：
- `reference_resolution_audit.csv`

#### Step C：把“原始监测记录”与“文献 DOI”绑定起来
形成可审计字段：
- `matched_doi`
- `matched_full_reference`
- `resolution_method`
- `local_pdf_found`
- `zotero_match_path`

#### Step D：再做 benchmark 子集过滤
只保留：
- `precipitation`
- `surface water`
- `drinking water`
- `waste water`
- `groundwater`
- `ice`
- `landfill leachate`

#### Step E：生成可用于比较的 TFA 数据库
输出：
- `tfa_literature_database_rebuilt.xlsx`
- `summary.json`

### 5.4 本次任务里，EST 的统计是怎么得出来的

#### 更宽的原始层
- `parsed_table_s1.csv` 原始记录：**169**
- 原始短引文献：**43**

#### 当前 benchmark 比较层
采用的是：
- water media
- 有 matched DOI
- 与当前 manual/model 对账逻辑一致

因此最终口径是：
- **26 篇文献 / 88 条记录**

并且这 **26 篇 / 88 条** 在本次统计中都已进入 `source_ready / 可比较` 状态，因此：
- 成功获取文献：**26**
- 成功获取记录：**88**

### 5.5 与后续提取工作的关系

`build_tfa_database.py` 重建的是 **文献库层**；
后续为了更细粒度复现 EST 图和 entry-level 记录，我们又额外做了 entry-level 重提取，例如：

- `/home/zhome/est_tfa_model_reextract_v4_confidence_20260330/est_tfa_entrylevel_records_v4_expanded.csv`

这个文件当前有：
- entry-level rows：**293**
- DOI 数：**11**

这说明：
- `tfa_est_rebuild` 负责的是**数据库重建与基线结构化**；
- entry-level reextract 负责的是**更细的点位 / 媒介 / 时段级复现**。

### 5.6 这个项目在本轮工作的定位

如果没有 `tfa_est_rebuild`，EST 这边后续所有统计都会出问题，因为你连“某条 TFA 水体记录到底属于哪篇 DOI 文献”都未必能稳定定义。

---

## 6. 项目三：`reference_extraction_audit` 是怎么展开的

项目路径：

- `/mnt/c/Users/Administrator/codex/projects/reference_extraction_audit`

核心脚本：

- `build_reference_extraction_audit.py`
- `build_nature_science_manual_vs_codex.py`

### 6.1 这个项目的职责

这个项目承担的是 **人工基准 vs 模型抽取结果** 的对账与差异归因。

也就是说：
- Nature / Science / EST 的提取结果并不是到此为止；
- 还必须进一步回答：
  - 覆盖率是多少？
  - 缺的是 PDF，还是缺的是数值？
  - 不一致是单位问题，还是 `<MDL` 之类非数值问题？
  - 哪些差异是“实际上 raw text 一样，只是我们不把它算 numeric exact”？

### 6.2 这一步具体怎么做

#### Step A：加载三个侧面的数据
- manual benchmark
- model outputs
- DOI / medium / compartment mapping

#### Step B：为三个库分别建立可比较 key
- Nature：DOI + compartment / matrix 语义
- Science：paper row + pollutant parameter
- EST：reference / DOI + medium + subcategory + location + year

#### Step C：按库分别做 manual vs model 审计
- coverage
- exact match
- missing-only
- model-only
- manual-only
- value diffs
- failure reason 分类

#### Step D：输出审计工作簿与 summary
- 工作簿
- summary JSON
- action items
- help-needed PDF lists

### 6.3 这个项目为什么在本轮特别重要

因为你这次不是只要“抽出来了没有”，而是要回答更细的问题，例如：

- Science 为什么 `pct_gt1` / `pct_gt10` 这么少？
- 为什么某些参数没有被纳入 1:1？
- 为什么 Nature / EST 的 `pollutant` 列先前其实只是统计字段名，而不是真污染物名？
- 为什么旧汇总表显示几乎全是 100% exact，而新长表不是？

这些问题都必须靠 audit 层回答，而不能靠 extraction 层硬解释。

### 6.4 Science 的长表重建就是这个层完成的

Science 这次最关键的对账成果包括：

- `/home/zhome/science_paired_long_all102.csv`
- `/home/zhome/science_paired_long_available95.xlsx`
- `/home/zhome/science_recheck_summary.xlsx`
- `/home/zhome/science_longtable_build_report.md`

#### 关键结果
- all102 long rows：**45,456**
- available95 long rows：**41,592**
- all102 unique paper-row ids：**1,894**
- available95 unique paper-row ids：**1,733**

#### 这一步实际解决了什么问题
1. 不再从旧汇总表反推原始值；
2. 每一行明确变成 `paper × parameter`；
3. `manual_raw`、`model_raw`、`manual_numeric`、`model_numeric` 同时保留；
4. `exact_1_to_1` 只对 **标准化 numeric 值完全一致** 的情况记 1；
5. `<MDL` / `ND` 之类不再错误算成 numeric exact。

### 6.5 Nature / EST 的 like-science 长表也在这个层收口

产出包括：
- `/home/zhome/nature_paired_long_pollutant_fixed.csv`
- `/home/zhome/est_paired_long_pollutant_fixed.csv`
- `/home/zhome/nature_est_paired_long_pollutant_fixed.xlsx`
- `/home/zhome/nature_est_pollutant_fix_note.md`

#### 当前语义修复后的结果
- Nature 当前人工 benchmark 在这个层面能稳定表达为：`sum_PFAS`
- EST 当前人工 benchmark 在这个层面能稳定表达为：`TFA`
- 统计字段（`min_conc`、`max_conc`、`avg_conc_ugL`、`pct_gt10`）被单独放入 `statistic_type`

也就是说，我们修复了之前一个关键语义错误：
> 原先 `pollutant` 列其实装的是统计量名，不是真污染物名。

---

## 7. Science 这条线为什么又额外拆出“图复现”工作

Science 这次不只是 1:1 对账，还要尽量复现图。

额外衍生工作包括：

- `build_science_longtables.py`
- `build_science_tables2_fig1b_and_yearprep.py`
- `build_science_image1_samplingyear.py`
- `build_science_image1_reconstruction.py`
- `/home/zhome/science_image1_reconstruction/...`

### 7.1 图复现为什么一开始失败

最核心的原因有两个：

1. **最初直接拿 publication year 当 x 轴年份**，导致 5 个 panel 形状过于相似；
2. **没有在 analyte / chemical group / sampling year 层做足够细的 groupmask 和 year assignment**，所以不同 panel 没有真正分开。

### 7.2 后来怎么修复

修复方向是：

- 回到 paper-level / analyte-level 长表；
- 尽量从本地 PDF 推 sampling year；
- 对只能得到 year range 的文献，做受控分配；
- 对 group 与 analyte 做白名单 / mask；
- Panel A 用 sampling year 而不是 publication year。

### 7.3 关键结果

- `science_image1_reconstruction_long_samplingyear_groupmask.csv`：**20,785** 行
- 覆盖 `reference_seq`：**102** 篇
- `science_image1_reconstruction_samplingyear_note.md` 已说明：
  - 95 篇有本地 PDF
  - 86 / 102 参考文献拥有非 publication-year 的 sampling-year 赋值
  - 9 篇只能保留 publication-year fallback
  - 7 篇因只有 publication-year-like 证据而被排除出推荐版 Panel A

这一步的意义是：
- Science 不再只是“参数一致率”项目；
- 还变成了一个“从 benchmark 表回推原图构成逻辑”的项目。

---

## 8. 三项工作之间的衔接关系

这三项工作不是三条完全独立的线，而是上下游衔接的。

### 8.1 上游：全文与源文本准备
由 `nature_science_extraction` 解决：
- PDF 是否存在
- PDF 是否完整
- 是否需要 DOI / browser fallback
- 模型拿到的原始 source text 是什么

### 8.2 中游：专门文献库重建
由 `tfa_est_rebuild` 解决：
- EST/TFA 的 reference → DOI → record 绑定
- 让附表型 benchmark 变成真正可计算数据库

### 8.3 下游：人工对账 / 长表 / 一致率 / 图复现
由 `reference_extraction_audit` 及其衍生脚本解决：
- long table
- exact 1:1
- within 2x / 3x
- missing / nonnumeric
- difference source attribution
- figure reconstruction support

---

## 9. 这三项工作里最容易混淆的几个口径

### 9.1 “文献数”不一定等于 DOI 数，也不一定等于 PDF 数
- 有的库按 DOI 聚合；
- 有的库按 reference short；
- 有的历史文件按 source_pdf；
- 有的汇总文件按 row_id。

所以这次统一时，优先采用：
- Science：benchmark paper id / reference seq
- Nature：`Nature2024_Refs` 中归一化 DOI
- EST：water subset 中 matched DOI

### 9.2 “记录数”也分很多层
同一个库可能同时存在：
- 原始 benchmark 行数
- paper-level 抽取结果数
- record-level 抽取数
- paper × parameter 长表行数
- analyte × year 图复现行数

因此不能把它们混在一起。

例如 Science：
- benchmark 记录数：**1894**
- long table 行数：**45,456**
- 图复现 sampling-year groupmask 行数：**20,785**

这三个都对，但不是一个层级。

### 9.3 `<MDL` / `ND` 不是 missing，也不是 numeric exact
这是 Science 对账中最关键的修正规则之一。

旧汇总里一些参数看起来几乎 100% 一致，原因不是模型真的全部抽对了，而是：
- 旧逻辑把 manual/model 同样写成 `<MDL` 的情况也算作 exact；
- 新长表逻辑要求：**只有标准化 numeric 值完全一致才算 exact_1_to_1 = 1**。

这也是为什么新长表和旧汇总在若干参数上会出现明显差异。

---

## 10. 这三项工作各自最终沉淀了什么

### 10.1 Nature / Science extraction 工程资产
- workflow
- profiles
- run manifest
- source snapshot
- browser fallback 机制
- PDF 完整性判断逻辑

### 10.2 EST rebuild 工程资产
- DOI resolution audit
- parsed monitoring table
- rebuilt workbook
- PDF inventory / Zotero 对齐信息
- 后续 entry-level 扩展基础

### 10.3 Audit / comparison 工程资产
- Science 逐条长表
- Nature / EST like-science 长表
- 汇总复核表
- 差异来源说明
- figure reconstruction supporting tables

---

## 11. 我建议后续在 README 中固定写清楚的内容

为了避免以后再出现“这个数字到底是哪一层”的混淆，我建议三个项目的 README 都固定包含以下四类信息：

1. **项目职责边界**  
   明确这个项目是做 retrieval、rebuild 还是 audit。

2. **统计口径说明**  
   明确 paper 数、record 数、long-table 行数分别是什么。

3. **关键输入 / 输出文件**  
   让后续任何人都知道要从哪里接。

4. **当前基准数字**  
   至少固定写出本轮这组最重要的数字：
   - Science：102 / 1894 → 95 / 1733
   - Nature：240 / 324 → 239 / 323
   - EST：26 / 88 → 26 / 88

---

## 12. 最终结论：这三项工作分别解决了什么问题

### Nature / Science extraction
解决的是：
> “我们到底有没有拿到足够好的 source，可以合法进入模型抽取和后续比较？”

### TFA EST rebuild
解决的是：
> “EST/TFA 的原始文献库能不能从附表和参考文献层稳定重建成可计算数据库？”

### Reference extraction audit
解决的是：
> “人工值和模型值到底在哪一层一致 / 不一致，差异是由什么造成的？”

把这三层串起来之后，三个文献库的工作才真正形成闭环：

- 先确定原始 benchmark；
- 再确认 source 覆盖；
- 然后做结构化抽取；
- 最后做逐条对账、长表、一致率和图复现。

---

## 13. 本轮最推荐保留的关键文件清单

### Nature / Science
- `/mnt/c/Users/Administrator/codex/projects/nature_science_extraction/workflow.py`
- `/mnt/c/Users/Administrator/codex/projects/nature_science_extraction/README.md`
- `/home/zhome/reextract_runs/study_metric_reextract_20260327_refresh/Nature/manifest.json`
- `/home/zhome/reextract_runs/study_metric_reextract_20260327_refresh/Science/manifest.json`

### Nature records
- `/home/zhome/nature_env_conc_extract/out_full_20260315_live/records_live.xlsx`

### Science paired tables / figure reconstruction
- `/home/zhome/science_paired_long_all102.csv`
- `/home/zhome/science_paired_long_available95.xlsx`
- `/home/zhome/science_recheck_summary.xlsx`
- `/home/zhome/science_longtable_build_report.md`
- `/home/zhome/science_image1_reconstruction/science_image1_reconstruction_long_samplingyear_groupmask.csv`

### EST rebuild
- `/mnt/c/Users/Administrator/codex/projects/tfa_est_rebuild/build_tfa_database.py`
- `/mnt/c/Users/Administrator/codex/projects/tfa_est_rebuild/parsed_table_s1.csv`
- `/mnt/c/Users/Administrator/codex/projects/tfa_est_rebuild/reference_resolution_audit.csv`
- `/mnt/c/Users/Administrator/codex/projects/tfa_est_rebuild/tfa_literature_database_rebuilt.xlsx`

### Cross-library audit
- `/mnt/c/Users/Administrator/codex/projects/reference_extraction_audit/build_reference_extraction_audit.py`
- `/mnt/c/Users/Administrator/codex/projects/reference_extraction_audit/build_nature_science_manual_vs_codex.py`
- `/mnt/c/Users/Administrator/codex/projects/reference_extraction_audit/summary.json`

---

## 14. 可直接写进对外说明的一句话版本

> 本轮三个文献库工作并不是单点提取，而是分别完成了：Nature / Science 的全文获取与结构化抽取工程、EST/TFA 的文献数据库重建工程，以及三库人工基准 vs 模型结果的长表化与误差审计工程；在统一 benchmark 口径下，当前统计为 Science 102/1894→95/1733，Nature 240/324→239/323，EST 26/88→26/88。

