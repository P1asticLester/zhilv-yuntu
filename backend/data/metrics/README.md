# 看板静态数据快照（6a）

## 这是什么

`GET /metrics/dashboard` 读的两份 CSV 的**仓库内静态快照**，来源是被 `.gitignore`
忽略的 `docs/tools/` 下的 A/B 运行产物。因为 `docs/` 不上传 GitHub，干净环境 / CI
读不到原始文件，所以把结果复制一份进仓库，保证接口在任何环境都有确定数据可读。

| 文件 | 源 | 批次 | 来源脚本 |
|---|---|---|---|
| `ablation_summary.csv` | `docs/tools/ablation_summary.csv` | 2026-11-08 16:49，18 条样例 × 3 轮中位数，9/9 全绿，`REDIS_ENABLED=false` 冷链路 | `docs/tools/run_ablation.py` |
| `timing_result.csv` | `docs/tools/timing_result.csv` | 3 次端到端 `/trip/generate` 计时（36.42 / 28.82 / 28.22s） | `docs/tools/bench_generate.py` |

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

```powershell
Copy-Item "D:\zhilv-yuntu\docs\tools\ablation_summary.csv" -Destination "D:\zhilv-yuntu\backend\data\metrics\ablation_summary.csv" -Force
Copy-Item "D:\zhilv-yuntu\docs\tools\timing_result.csv" -Destination "D:\zhilv-yuntu\backend\data\metrics\timing_result.csv" -Force
```

只想临时看本机最新数据、不覆盖快照时，给后端进程设环境变量：

```powershell
$env:METRICS_DATA_DIR = "D:\zhilv-yuntu\docs\tools"
```

## 缺失时会怎样

接口不会 500，也不会拿旧数字假装稳定：对应分段的 `available` 变 `false`、
`rows` 为空、`sources` 里标 `kind=missing`，顶层 `status` 变 `partial` 或 `empty`。
`trip_stats` 段不依赖这两份文件（直接读本地 SQLite 单表），不受影响。
