# Nature / Science DOI-PDF Extraction Workflow

本项目负责 **Nature2024 / Science2020** 两个文献库的全文获取、PDF 完整性判断、网页回退、源文本准备以及结构化抽取入口。

> 详细总说明见：[`../reference_extraction_audit/THREE_LIBRARY_EXECUTION_WORKFLOW_20260331.md`](../reference_extraction_audit/THREE_LIBRARY_EXECUTION_WORKFLOW_20260331.md)

---

## 1. 这个项目在三库工作中的定位

三库工作这次被拆成三个项目：

1. `nature_science_extraction`：负责 Nature / Science 的 DOI → PDF / source text → structured extraction
2. `tfa_est_rebuild`：负责 EST / TFA 的原始文献库重建与 DOI 匹配
3. `reference_extraction_audit`：负责人工基准 vs 模型输出的长表、覆盖率和误差审计

本项目属于**上游工程层**，核心任务不是直接给出最终一致率，而是先确保：

- 文献是否真的拿到本地 PDF
- PDF 是否是完整正文，而不是只有首页 / 摘要 / metadata
- DOI / 标题 / 本地 PDF / 网页回退是否能稳定落到一个可抽取 source
- source text snapshot 是否保留，后续是否可复核

如果没有这一层，后续 Nature / Science 的 manual-vs-model 比较会失去可解释性。

---

## 2. 当前与本轮任务直接相关的统计口径

### Nature（benchmark 比较口径）

来自 `four_studies_reference_database_with_doi.xlsx` 的 `Nature2024_Refs`，并且只保留：

- DOI 可识别
- 非 `PRODUCT*`
- 水介质：`SW / GW / DW_RAW / WW / INFLUENT / EFFLUENT / LEACHATE`

当前统计：

- 原始 `Nature2024_Refs`：`504` 行
- 有 DOI：`396` 行
- 非 PRODUCT 且 water：`324` 行 / `240` 篇 DOI 文献
- 其中已进入 `source_ready`：`239` 篇 / `323` 条记录

另一个更宽的 PDF 工作流口径：

- `reextract_runs/study_metric_reextract_20260327_refresh/Nature/manifest.json`
- manifest 总 paper 数：`327`
- `source_ready`：`276`
- `no_source`：`51`

> 注意：`327` 是 PDF 工作流 paper 数，不等于当前 benchmark 比较口径的 `240`。

### Science（benchmark 比较口径）

当前统计：

- 总文献：`102`
- 原始记录：`1894`
- 已拿到本地 PDF 的文献：`95`
- available95 记录：`1733`

这套 `102 / 1894 -> 95 / 1733` 是后续 Science paired long table 的统一口径。

---

## 3. 这个项目实际做了什么

### Step 1：读取 benchmark 输入并生成 paper inputs

入口来自 Excel / DOI 表，按 `source_block` 选择：

- `Nature2024`
- `Science2020`
- 兼容：`Water2023`、`Lancet2023`、`NatComm2017`

核心字段包括：

- `row_id`
- `record_id`
- `study_title`
- `citation_text`
- `doi`
- `retrieval_key`

### Step 2：本地 PDF 候选检索

脚本会优先在对应文献库目录中寻找本地 PDF，并对候选进行排序。

### Step 3：PDF 完整性判断

不会因为“找到了一个 PDF 文件”就直接使用，而是继续检查：

- 页数是否正常（`pdfinfo`）
- `pdftotext` 能否抽出足够正文
- 是否包含 `abstract / introduction / results / discussion / references / table / figure` 等 body hints
- 是否像“只有一页 / 只有预览 / 只有摘要”

### Step 4：网页回退（web fallback）

当本地 PDF 缺失或被判定为不完整时，工作流会继续尝试：

1. DOI landing page
2. Scholar / Google
3. 浏览器人工协助
4. 保留 source snapshot，保证后续可追溯

### Step 5：按 profile 生成结构化抽取结果

- Nature 使用环境浓度记录型 schema
- Science 使用 monitoring-summary / benchmark-parameter schema

### Step 6：输出 run assets

每次运行都会保留：

- manifest
- run summary
- source text snapshot
- 单篇 JSON
- merged JSON / merged CSV（视 profile 而定）

---

## 4. 与本项目直接相关的关键文件

### 核心脚本

- `workflow.py`
- `profiles.py`
- `run_nature_science_batch.sh`

### 关键运行结果

- `runs/<run_id>/...`
- `latest_status.json`

### 本轮重点引用文件

- Nature manifest：`/home/zhome/reextract_runs/study_metric_reextract_20260327_refresh/Nature/manifest.json`
- Science manifest：`/home/zhome/reextract_runs/study_metric_reextract_20260327_refresh/Science/manifest.json`
- Science available95 records：`/home/zhome/sciadv_2024_library/batch_outputs/sciadv_2024_seawater_records_available_pdfs.csv`

---

## 5. 为什么这个项目对三库闭环是必须的

后续所有 manual-vs-model 分析都依赖本项目先回答下面几个问题：

1. 某篇文献是否真的拿到了 source？
2. 这个 source 是完整正文，还是只有一页？
3. 抽取失败时，原因是 source 缺失、PDF 不完整、网页失败，还是模型本身失败？
4. 现在看到的 model output，到底基于哪一个 source text？

也就是说，本项目负责解决的是：

> “我们到底有没有拿到足够好的 source，可以进入模型抽取和后续比较？”

---

## 6. 常用命令

### 计数查看

```bash
cd /mnt/c/Users/Administrator/codex/projects/nature_science_extraction
python3 workflow.py --print-counts
```

### Nature 单篇试跑

```bash
python3 workflow.py --source-block Nature2024 --limit 1
```

### Science 单篇试跑

```bash
python3 workflow.py --source-block Science2020 --limit 1
```

### 处理某一条特定记录

```bash
python3 workflow.py --source-block Nature2024 --row-id DOI0001
```

### 跳过已有 JSON 的批量运行

```bash
python3 workflow.py --source-block Nature2024 --extractor codex --skip-existing-json --no-browser --max-source-chars 20000 --codex-timeout-seconds 300
python3 workflow.py --source-block Science2020 --extractor codex --skip-existing-json --no-browser --max-source-chars 20000 --codex-timeout-seconds 300
```

### 从旧 run 中重试 one-page / failed 论文

```bash
python3 workflow.py --from-run <run_id> --status-filter failed --failure-reason-contains "pdf has one page or less" --extractor codex --max-source-chars 12000 --codex-timeout-seconds 240
```

---

## 7. 当前建议

1. README 中固定保留 benchmark 口径数字，不要只保留 PDF 工作流数字。
2. Nature / Science 后续所有一致率、paired long table、图复现，都应回链到本项目的 manifest/source snapshot。
3. 不要把“找到 PDF”和“成功进入可比较提取流程”混为一谈。
