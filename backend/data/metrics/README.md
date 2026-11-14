# 看板静态数据快照（6a）

## 这是什么

`GET /metrics/dashboard` 读的两份 CSV 的**仓库内静态快照**：它们是 2026-11-08
那批 A/B / 端到端计时的运行结果，原始产物留在被 `.gitignore` 忽略的
`docs/tools/`。因为 `docs/` 不上传 GitHub，干净环境 / CI 读不到原始文件，
所以把结果复制一份进仓库，保证接口在任何环境都有确定数据可读。

**2026-11-14（Step 1）**：产出这些结果的工具脚本已从 `docs/tools/` 迁进
`backend/scripts/`（`bench_generate.py` / `run_ablation.py` /
`run_cache_ablation.py` / `analyze_retrieval_redundancy.py`），可当场 `--help` 复现；
`docs/tools/` 原件与历史日志、汇总 CSV/MD 仍留本机。新脚本默认写
`backend/scripts/_local/`（被 `.gitignore` 忽略），**不会**覆盖本目录的两份快照。

| 文件 | 源 | 批次 | 来源脚本 |
|---|---|---|---|
| `ablation_summary.csv` | `docs/tools/ablation_summary.csv` | 2026-11-08 16:49，18 条样例 × 3 轮中位数，9/9 全绿，`REDIS_ENABLED=false` 冷链路 | `backend/scripts/run_ablation.py`（原 `docs/tools/`） |
| `timing_result.csv` | `docs/tools/timing_result.csv` | 3 次端到端 `/trip/generate` 计时（36.42 / 28.82 / 28.22s） | `backend/scripts/bench_generate.py`（原 `docs/tools/`） |

两份文件与源文件**逐字节一致**（含 UTF-8 BOM），接口按 `utf-8-sig` 读取。

## 口径（别读错）

- `avg_latency_ms` 只量检索阶段（embedding 召回 + rerank），**不是**端到端耗时，
  也不含 LLM 改写；端到端看 `timing_result.csv` 的 `seconds`。
- `MRR` / `top1_pct` 等是 18 条样例 × 3 轮的中位数；历史 93.3% 是 15 条样例时期
  且模型配置不同，不与这里的 88.9% 直接比较。
- 关 rerank 的收益要连着质量损失讲（top1 88.9%→83.3%、MRR 0.928→0.889），
  **不要**写成「快了 43%」。
- 这是**历史批次的静态快照，不是实时测量**。重跑 A/B 后需手动刷新（见下）。

## 刷新方式（PowerShell）

**覆盖前先备份**，保持 `utf-8-sig`(BOM) 与逐字节口径，并在 CHANGELOG 标注
样本数与环境。最稳妥的做法是从已核过的本地汇总复制：

```powershell
Copy-Item "D:\zhilv-yuntu\docs\tools\ablation_summary.csv" -Destination "D:\zhilv-yuntu\backend\data\metrics\ablation_summary.csv" -Force
Copy-Item "D:\zhilv-yuntu\docs\tools\timing_result.csv" -Destination "D:\zhilv-yuntu\backend\data\metrics\timing_result.csv" -Force
```

或用仓库内脚本重新跑出明细后显式指过来（需后端在线 + 真实 API Key，属在线评估范畴）：

```powershell
& "D:\zhilv-yuntu\backend\.venv\Scripts\python.exe" "D:\zhilv-yuntu\backend\scripts\bench_generate.py" --out "D:\zhilv-yuntu\backend\data\metrics\timing_result.csv"
```

脚本默认写 `backend/scripts/_local/`，只有显式 `--out` / `--out-dir` 才会落到
本目录，避免误覆盖证据链。

只想临时看本机最新数据、不覆盖快照时，给**后端进程**设环境变量：

```powershell
$env:METRICS_DATA_DIR = "D:\zhilv-yuntu\docs\tools"
```

**别混用两个变量**：`METRICS_DATA_DIR` 由 `metrics_service.py` 使用，决定「看板
读哪个目录」；`BENCH_OUTPUT_DIR` / `BENCH_LOGS_DIR` 只决定「度量脚本把产物写到哪」。

## 缺失时会怎样

接口不会 500，也不会拿旧数字假装稳定：对应分段的 `available` 变 `false`、
`rows` 为空、`sources` 里标 `kind=missing`，顶层 `status` 变 `partial` 或 `empty`。
`trip_stats` 段不依赖这两份文件（直接读本地 SQLite 单表），不受影响。
