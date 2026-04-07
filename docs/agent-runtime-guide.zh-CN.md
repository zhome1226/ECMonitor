# ECMonitor 多智能体运行说明（中文）

这份文档说明 ECMonitor 各个模块当前是如何运行的、每个模块读什么/写什么，以及在当前沙盒环境下默认内置了什么、还缺什么。

当前仓库是一个**轻量级多智能体脚手架**：

- 六个 specialist agent 的职责和交接边界已经明确
- `Validation / Analytics / Platform` 已经有可直接运行的 starter scripts
- `Retrieval` 和 `Extraction` 目前以技能、辅助脚本和文档约定为主
- 大多数 starter scripts 故意只依赖 Python 标准库，方便在最小环境中运行

---

## 1. 端到端运行顺序

ECMonitor 的目标链路是：

1. `ResearchManager`
2. `RetrievalSpecialist`
3. `ExtractionSpecialist`
4. `ValidationSpecialist`
5. `AnalyticsSpecialist`
6. `PlatformSpecialist`

当前仓库中的集成脚手架是：

- `agents/literature-db-builder/scripts/run_benchmark_workflow.py`

示例：

```bash
python3 agents/literature-db-builder/scripts/run_benchmark_workflow.py \
  --benchmark Science \
  --targets targets.csv \
  --run-root ./runs
```

它目前能做的事情：

- 创建 run 目录
- 写出 `summary.json`
- 记录预期的阶段和工件

它目前**还不会**做的事情：

- 不会自动依次调用六个 agent
- 目前它还是 workflow scaffold，不是完整工作流引擎

---

## 2. ResearchManager

位置：

- `agents/research-manager/AGENT.md`
- `agents/research-manager/references/task-definition.md`

职责：

- 定义 benchmark/任务族
- 定义目标行结构（row shape）
- 定义纳入/排除规则
- 定义 release criteria

当前运行形态：

- **文档驱动**
- 还没有独立可执行脚本

典型输入：

- benchmark 文献或 benchmark 来源表
- 项目 brief
- 历史 run notes

典型输出：

- `task_brief.md`
- benchmark rulebook
- target selection notes

沙盒假设：

- 不依赖额外二进制
- 不依赖网络
- 当前主要由人工或上层编排器完成

---

## 3. RetrievalSpecialist

位置：

- `agents/retrieval-specialist/AGENT.md`
- `agents/retrieval-specialist/references/source-gate.md`
- `skills/fulltext-retrieval/`

核心辅助脚本：

- `skills/fulltext-retrieval/scripts/check_pdf_integrity.py`
- `skills/fulltext-retrieval/scripts/publisher_api_harvest.py`

### 3.1 检索顺序

当前 Retrieval 约定按下面的顺序依次尝试，并在第一个成功来源处停止：

1. open / open-access PDF
2. publisher API harvest
3. Zotero / institutional access
4. browser-assisted retrieval

### 3.2 PDF 完整性检查

示例：

```bash
python3 skills/fulltext-retrieval/scripts/check_pdf_integrity.py article.pdf
```

输入：

- 本地 PDF 路径

输出：

- 输出到 stdout 的 JSON，通常包括：
  - `pages`
  - `extractable`
  - `integrity_status`

当前沙盒/运行要求：

- Python 3 标准库
- 系统二进制：
  - `pdfinfo`
  - `pdftotext`

如果系统里没有这些二进制：

- 页数可能拿不到
- 文本可抽取性检查可能失败

### 3.3 Publisher API harvester

示例：

```bash
python3 skills/fulltext-retrieval/scripts/publisher_api_harvest.py \
  --input dois.txt \
  --output-dir ./downloads
```

输入：

- DOI 文件

输出：

- 成功下载的 PDF
- 检索结果 manifest / 行级结果

当前沙盒/运行要求：

- Python 3
- 第三方包：
  - `requests`
- 需要网络
- 可能需要环境变量：
  - `ELSEVIER_API_KEY`
  - `SPRINGER_API_KEY`
  - `WILEY_TDM_TOKEN`

当前说明：

- Retrieval 是整个仓库里最依赖网络和外部工具的模块
- 浏览器辅助获取目前主要还是文档流程，不是全自动脚本

---

## 4. ExtractionSpecialist

位置：

- `agents/extraction-specialist/AGENT.md`
- `agents/extraction-specialist/references/evidence-contract.md`
- `skills/llm-extraction/`

职责：

- 从 PDF、HTML、supplement 或 benchmark 来源表中提取结构化字段

当前运行形态：

- 以 **prompt/profile 驱动** 为主
- 仓库里还没有完整独立的 extraction executor

推荐运行方式：

1. 选择 benchmark profile：
   - `skills/llm-extraction/references/profiles.md`
2. 读取对应 prompt：
   - `prompt_nature.md`
   - `prompt_science.md`
   - `prompt_est_tfa.md`
3. 把验证后的 PDF / HTML / supplement / 来源表交给 LLM
4. 输出带 provenance 的结构化记录

典型输入：

- 已验证的源文档或 benchmark 来源表
- benchmark profile
- schema / evidence contract

典型输出：

- 结构化 extracted rows
- 证据页码 / 图表位置
- unresolved notes

沙盒假设：

- 当前仓库把 Extraction 视为 **LLM 抽取协议层**，而不是本地解析器
- 只阅读 prompt/profile 文档本身不需要额外依赖
- 如果要真正自动跑 extraction，仍然需要在仓库外或后续补充 LLM caller

---

## 5. ValidationSpecialist

位置：

- `agents/validation-specialist/AGENT.md`
- `agents/validation-specialist/scripts/validate_extraction_batch.py`
- `agents/validation-specialist/references/rulebooks/*.json`

职责：

- 审核 extracted rows
- 做字段完整性、重复、冲突、禁用字段、review/reject 分流

### 5.1 当前沙盒里内置了什么

Validation starter script 只使用 Python 3 标准库：

- `argparse`
- `csv`
- `json`
- `collections`
- `pathlib`

它**不需要**：

- pandas
- numpy
- sklearn
- 网络

因此 Validation 目前可以在离线环境中直接运行。

### 5.2 如何运行

显式指定 rulebook：

```bash
python3 agents/validation-specialist/scripts/validate_extraction_batch.py \
  --input examples/benchmark-instances/science_advances_legacy_pops_global_ocean/sample_results.json \
  --rulebook agents/validation-specialist/references/rulebooks/science_synthesis_rulebook.json \
  --out-dir /tmp/ecmonitor_validation
```

自动选择 rulebook：

```bash
python3 agents/validation-specialist/scripts/validate_extraction_batch.py \
  --input examples/benchmark-instances/science_advances_legacy_pops_global_ocean/sample_results.json \
  --benchmark science \
  --out-dir /tmp/ecmonitor_validation
```

### 5.3 它读取什么

- extracted rows JSON 数组

### 5.4 它输出什么

- `validation_audit.csv`
- `validated_rows.json`
- `review_rows.json`
- `rejected_rows.json`
- `summary.json`
- `rulebook_manifest.json`

### 5.5 当前内置规则能力

- required fields
- required-any field groups
- duplicate detection
- forbidden fields
- review vs reject 逻辑

当前仓库自带 rulebooks：

- `science_synthesis_rulebook.json`
- `nature_record_rulebook.json`
- `est_reference_rebuild_rulebook.json`

---

## 6. AnalyticsSpecialist

位置：

- `agents/analytics-specialist/AGENT.md`
- `agents/analytics-specialist/scripts/fetch_context_adapters.py`
- `agents/analytics-specialist/scripts/run_analysis_skeleton.py`
- `agents/analytics-specialist/references/external-context.md`

职责：

- 接入外部上下文和数据库接口
- 汇总 validated rows
- 生成 enriched rows 和基础分析结果

### 6.1 分析专家当前沙盒里内置了什么

当前 Analytics starter scripts 只依赖 Python 3 标准库：

- `argparse`
- `csv`
- `json`
- `collections`
- `pathlib`
- `statistics`
- `urllib`
- `re`
- `os`

这意味着：

- 默认可以离线运行
- 不需要 pandas / sklearn / requests
- context fetch 直接使用内置 `urllib`
- 只有加 `--fetch` 时才需要网络

所以如果你问“AnalyticsSpecialist 在当前沙盒里内置了什么”，最准确的回答是：

- **内置的是 Python 标准库级别的轻量分析能力**
- **默认不包含重型科学计算栈**
- **默认不包含数据库服务进程**
- **可以离线跑基础统计、汇总和上下文合并**

### 6.2 Context adapter 阶段

命令：

```bash
python3 agents/analytics-specialist/scripts/fetch_context_adapters.py \
  --input /tmp/ecmonitor_validation/validated_rows.json \
  --out-dir /tmp/ecmonitor_context
```

启用在线抓取：

```bash
python3 agents/analytics-specialist/scripts/fetch_context_adapters.py \
  --input /tmp/ecmonitor_validation/validated_rows.json \
  --out-dir /tmp/ecmonitor_context \
  --fetch
```

它做的事情：

- 提取 DOI 列表
- 提取 country 候选
- 构建对这些源的请求清单：
  - Crossref
  - OpenAlex
  - World Bank

当启用 `--fetch` 时：

- 调远程 API
- 保存原始返回
- 归一化为：
  - `normalized/crossref_works.json`
  - `normalized/openalex_works.json`
  - `normalized/world_bank_records.json`
  - `normalized/world_bank_latest.json`
  - `normalized/context_summary.json`

可选环境变量：

- `OPENALEX_API_KEY`

网络说明：

- `--fetch` 需要网络
- 不加 `--fetch` 时，脚本只生成 manifest，适合离线模式

### 6.3 Analysis 阶段

命令：

```bash
python3 agents/analytics-specialist/scripts/run_analysis_skeleton.py \
  --input /tmp/ecmonitor_validation/validated_rows.json \
  --context-manifest /tmp/ecmonitor_context/context_manifest.json \
  --out-dir /tmp/ecmonitor_analytics
```

它读取：

- `validated_rows.json`
- 可选 `context_manifest.json`
- manifest 中引用到的 normalized context outputs

它输出：

- `analytics_summary.json`
- `pollutant_summary.csv`
- `matrix_summary.csv`
- `geography_summary.csv`
- `enriched_rows.json`
- 可选复制 `context_manifest.json`

当前已内置的分析能力：

- count summaries
- trend summaries
- numeric min/max/mean
- 行级 context merge：
  - Crossref
  - OpenAlex
  - World Bank latest indicators

当前还**没有**内置的能力：

- regression models
- ML pipelines
- SHAP
- 数据库服务化
- 生产级模型推理编排

---

## 7. PlatformSpecialist

位置：

- `agents/platform-specialist/AGENT.md`
- `agents/platform-specialist/scripts/build_publication_bundle.py`
- `agents/platform-specialist/references/publication_bundle_schema.json`
- `agents/platform-specialist/references/public_api_schema.json`
- `agents/platform-specialist/references/api-contract.md`

职责：

- 把 validated 和 analytics 输出打包成可发布 bundle
- 为未来数据库加载、网站适配、API 发布提供统一出口

### 7.1 当前沙盒里内置了什么

Platform starter script 只使用 Python 3 标准库：

- `argparse`
- `json`
- `shutil`
- `datetime`
- `pathlib`

它**不需要**：

- 网络
- 第三方 Python 包

因此 Platform 目前也可以完全离线运行。

### 7.2 如何运行

```bash
python3 agents/platform-specialist/scripts/build_publication_bundle.py \
  --validated /tmp/ecmonitor_validation/validated_rows.json \
  --analytics-dir /tmp/ecmonitor_analytics \
  --context-manifest /tmp/ecmonitor_context/context_manifest.json \
  --out-dir /tmp/ecmonitor_publication \
  --release-tag demo-v1
```

### 7.3 它读取什么

- `validated_rows.json`
- analytics 输出目录
- 可选 `context_manifest.json`

### 7.4 它输出什么

- `api/validated_rows.json`
- `api/analytics_summary.json`
- `api/context_manifest.json`（如果提供）
- `api/enriched_rows.json`（如果存在）
- `api/publication_payload.json`
- `reports/pollutant_summary.csv`
- `reports/matrix_summary.csv`
- `reports/geography_summary.csv`
- `release_manifest.json`
- `release_notes.md`
- `site_index.json`
- `publication_bundle_schema.json`
- `public_api_schema.json`
- `README.md`

### 7.5 这个 bundle 的用途

当前 Platform 生成的是一个中立的发布交接包，用于：

- 后续数据库导入
- 网站/API adapter 对接
- 前端 demo 或公开平台发布

它现在还不是完整 Web App，但已经定义了：

- bundle contract
- API contract
- release payload entrypoint

---

## 8. 当前“沙盒内置”与“可选外部依赖”

### 当前 runnable skeleton 里内置的部分

- Python 3 标准库：
  - Validation
  - Analytics
  - Platform
- Extraction 的 prompt/profile 体系
- Retrieval 的检索顺序和辅助脚本

### 可选 / 外部依赖

- `requests`：用于 `publisher_api_harvest.py`
- `pdfinfo`、`pdftotext`：用于 PDF 完整性检查
- publisher API keys：
  - Elsevier
  - Springer
  - Wiley
- `OPENALEX_API_KEY`：OpenAlex 可选
- 网络：
  - publisher API 抓取
  - Crossref/OpenAlex/World Bank 在线抓取

### 当前还未作为一等本地运行时内置的部分

- 完整 LLM extraction caller
- 自动化浏览器检索执行器
- pandas/sklearn 等重型分析栈
- 生产级网站前端和部署层

---

## 9. 推荐的安全本地运行顺序

如果你希望当前仓库在**最小沙盒、完全离线**条件下跑通，建议用下面的顺序：

1. 准备或复用 extracted rows JSON
2. 运行 Validation
3. 运行 Analytics（不要加 `--fetch`）
4. 运行 Platform

示例：

```bash
python3 agents/validation-specialist/scripts/validate_extraction_batch.py \
  --input examples/benchmark-instances/science_advances_legacy_pops_global_ocean/sample_results.json \
  --benchmark science \
  --out-dir /tmp/ecmonitor_validation

python3 agents/analytics-specialist/scripts/fetch_context_adapters.py \
  --input /tmp/ecmonitor_validation/validated_rows.json \
  --out-dir /tmp/ecmonitor_context

python3 agents/analytics-specialist/scripts/run_analysis_skeleton.py \
  --input /tmp/ecmonitor_validation/validated_rows.json \
  --context-manifest /tmp/ecmonitor_context/context_manifest.json \
  --out-dir /tmp/ecmonitor_analytics

python3 agents/platform-specialist/scripts/build_publication_bundle.py \
  --validated /tmp/ecmonitor_validation/validated_rows.json \
  --analytics-dir /tmp/ecmonitor_analytics \
  --context-manifest /tmp/ecmonitor_context/context_manifest.json \
  --out-dir /tmp/ecmonitor_publication \
  --release-tag demo-v1
```

---

## 10. 当前成熟度

- `ResearchManager`：规则/文档阶段
- `RetrievalSpecialist`：helper-script 阶段
- `ExtractionSpecialist`：prompt/profile 阶段
- `ValidationSpecialist`：可运行 skeleton
- `AnalyticsSpecialist`：可运行 skeleton，支持可选在线 context fetch
- `PlatformSpecialist`：可运行 skeleton，支持 bundle/schema 输出

这意味着仓库当前已经适合：

- benchmark 设计
- 文献证据审核
- 轻量分析
- 发布包生成

但后续还需要继续补强：

- Retrieval 全自动化
- Extraction 真正的运行时
- 重型分析模型
- 生产部署层
