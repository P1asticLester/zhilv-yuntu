<script setup lang="ts">
/**
 * 6b · Token 成本分析看板（只读）
 *
 * 数据源：GET /metrics/dashboard（6a 后端聚合接口）。
 * 契约：types/index.ts 的 MetricsDashboardResponse，与 Pydantic 一一对应。
 *
 * 口径约束（刻意写进界面，避免误读）：
 * - 三段数据都可能缺失，available=false 时显示空状态与 note，绝不拿 0 冒充真实数据；
 * - 数字原样展示，不派生「快了 N%」「省了多少」这类结论；
 * - token 构成条只表示「同一模式内各环节的占比」，不是节省量、不是优化收益；
 * - 质量（top1 / MRR）与成本同屏，避免只看成本得出「关掉某个环节更好」。
 */
import { onMounted, ref, watch, computed } from "vue";

import { fetchMetricsDashboard } from "../services/api";
import type {
  AblationRow,
  MetricsDashboardResponse,
  MetricsDashboardStatus,
  MetricsSourceKind,
} from "../types";

const props = defineProps<{
  active: boolean;
}>();

const loading = ref(false);
const error = ref("");
const data = ref<MetricsDashboardResponse | null>(null);

const MODE_LABEL: Record<string, string> = {
  base: "baseline（全开）",
  norew: "--no-rewrite（关 LLM 改写）",
  norr: "--no-rerank（关重排）",
};

const STATUS_TEXT: Record<MetricsDashboardStatus, string> = {
  ok: "三段数据齐全",
  partial: "部分数据可用",
  empty: "全部数据缺失",
};

const KIND_TEXT: Record<MetricsSourceKind, string> = {
  snapshot: "静态快照",
  live: "实时服务层",
  missing: "缺失",
};

const SOURCE_TEXT: Record<string, string> = {
  ablation: "RAG 消融 A/B 汇总",
  timing: "端到端计时",
  trip_stats: "已保存行程 token 统计",
};

function modeLabel(mode: string): string {
  return MODE_LABEL[mode] ?? mode;
}

function fmt(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  // 至少保留 2 位：端到端中位数 28.82s、单次 36.42 / 28.22 都是两位小数的既定口径，
  // 截成 1 位会与文档/CSV 数字对不上；整数仍显示千分位且不带多余小数。
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

/** 同一模式内 prompt token 三段的占比，仅用于构成展示，不与其它模式做收益比较。 */
function promptParts(row: AblationRow) {
  const parts = [
    { key: "embedding", label: "Embedding", value: row.embedding_tokens, color: "#5AC8FA" },
    { key: "rerank", label: "Rerank", value: row.rerank_tokens, color: "#007AFF" },
    { key: "rewrite", label: "LLM 改写", value: row.rewrite_prompt_tokens, color: "#AF52DE" },
  ];
  const total = parts.reduce((sum, item) => sum + item.value, 0);
  return {
    total,
    parts: parts.map((item) => ({
      ...item,
      pct: total > 0 ? (item.value / total) * 100 : 0,
    })),
  };
}

const hasAnyData = computed(() => {
  const d = data.value;
  if (!d) return false;
  return d.ablation.available || d.timing.available || d.trip_stats.available;
});

async function loadDashboard() {
  loading.value = true;
  error.value = "";
  try {
    data.value = await fetchMetricsDashboard();
  } catch (err) {
    console.error(err);
    data.value = null;
    error.value = "看板数据加载失败。请确认后端已启动，且 GET /metrics/dashboard 可访问。";
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  if (props.active) void loadDashboard();
});

watch(() => props.active, (active) => {
  if (active) void loadDashboard();
});
</script>

<template>
  <section class="metrics-page">
    <!-- 头部 -->
    <div class="ios-card metrics-header">
      <div>
        <h2 class="metrics-header__title">Token 成本分析看板</h2>
        <p class="metrics-header__desc">
          对比不同 RAG 策略下的 token 消耗、检索质量与端到端耗时
        </p>
      </div>
      <div class="metrics-header__actions">
        <span
          v-if="data"
          :class="['status-badge', `status-badge--${data.status}`]"
        >
          {{ STATUS_TEXT[data.status] }}
        </span>
        <button
          class="ios-btn ios-btn--primary ios-btn--sm"
          :disabled="loading"
          @click="loadDashboard"
        >
          {{ loading ? "加载中..." : "刷新" }}
        </button>
      </div>
    </div>

    <!-- 加载 / 失败 / 空 -->
    <div v-if="loading && !data" class="ios-card ios-empty">正在加载...</div>
    <div v-else-if="error" class="ios-card ios-empty ios-empty--error">
      <p>{{ error }}</p>
      <button class="ios-btn ios-btn--primary ios-btn--sm" @click="loadDashboard">重试</button>
    </div>
    <div v-else-if="!data || !hasAnyData" class="ios-card ios-empty">
      暂无可用数据。后端数据源状态会标注在「数据来源」里。
    </div>

    <template v-else-if="data">
      <!-- ① RAG 消融 A/B -->
      <div class="ios-card">
        <div class="section-head">
          <div>
            <h3 class="section-title">RAG 消融 A/B 汇总</h3>
            <p class="section-sub">18 条样例 × 3 轮中位数 · REDIS_ENABLED=false 冷链路</p>
          </div>
          <span v-if="!data.ablation.available" class="tag tag--missing">数据缺失</span>
        </div>

        <div v-if="!data.ablation.available" class="section-empty">
          {{ data.ablation.note || "A/B 汇总快照不可用。" }}
        </div>
        <template v-else>
          <div class="table-scroll">
            <table class="metrics-table">
              <thead>
                <tr>
                  <th>模式</th>
                  <th>成功轮数</th>
                  <th>top1</th>
                  <th>topK</th>
                  <th>MRR</th>
                  <th>检索耗时 (ms)</th>
                  <th>Rerank token</th>
                  <th>改写 token</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in data.ablation.rows" :key="row.mode">
                  <td class="cell-mode">{{ modeLabel(row.mode) }}</td>
                  <td>{{ fmt(row.rounds_ok) }}</td>
                  <td>{{ row.top1_pct.toFixed(1) }}%</td>
                  <td>{{ row.topk_pct.toFixed(1) }}%</td>
                  <td>{{ row.mrr.toFixed(3) }}</td>
                  <td>{{ fmt(row.avg_latency_ms) }}</td>
                  <td>{{ fmt(row.rerank_tokens) }}</td>
                  <td>{{ fmt(row.rewrite_prompt_tokens) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="section-sub section-sub--foot">
            检索耗时只量 embedding 召回 + rerank，不含 LLM 改写，也不是端到端耗时。
          </p>

          <!-- token 构成：同一模式内占比，不是节省量 -->
          <div class="bars">
            <div class="bars__head">
              <span class="bars__title">各环节 prompt token 构成</span>
              <span class="bars__hint">同一模式内的占比，不是节省量、不是优化收益</span>
            </div>
            <div v-for="row in data.ablation.rows" :key="`bar-${row.mode}`" class="bar-row">
              <div class="bar-row__label">{{ modeLabel(row.mode) }}</div>
              <div class="bar-row__track">
                <div
                  v-for="part in promptParts(row).parts"
                  :key="part.key"
                  class="bar-seg"
                  :style="{ width: `${part.pct}%`, background: part.color }"
                  :title="`${part.label}: ${fmt(part.value)} (${part.pct.toFixed(1)}%)`"
                />
              </div>
              <div class="bar-row__total">{{ fmt(promptParts(row).total) }}</div>
            </div>
            <div class="bars__legend">
              <span><i style="background:#5AC8FA" />Embedding</span>
              <span><i style="background:#007AFF" />Rerank</span>
              <span><i style="background:#AF52DE" />LLM 改写</span>
            </div>
          </div>
        </template>
      </div>

      <!-- ② 端到端计时 -->
      <div class="ios-card">
        <div class="section-head">
          <div>
            <h3 class="section-title">端到端 /trip/generate 计时</h3>
            <p class="section-sub">3 次真实请求，从发请求到读完响应体</p>
          </div>
          <span v-if="!data.timing.available" class="tag tag--missing">数据缺失</span>
        </div>

        <div v-if="!data.timing.available" class="section-empty">
          {{ data.timing.note || "端到端计时快照不可用。" }}
        </div>
        <template v-else>
          <div class="stat-grid">
            <div class="stat-box">
              <div class="stat-box__label">耗时中位数</div>
              <div class="stat-box__value">
                {{ data.timing.median_seconds === null ? "—" : fmt(data.timing.median_seconds) }}
                <span class="stat-box__unit">s</span>
              </div>
              <div class="stat-box__note">3 次取中位数，首次偏慢为冷启动</div>
            </div>
            <div class="stat-box">
              <div class="stat-box__label">成功次数</div>
              <div class="stat-box__value">
                {{ data.timing.rows.filter((r) => r.status === 200 && !r.error).length }}
                <span class="stat-box__unit">/ {{ data.timing.rows.length }}</span>
              </div>
              <div class="stat-box__note">HTTP 200 且无错误信息</div>
            </div>
          </div>
          <div class="table-scroll">
            <table class="metrics-table">
              <thead>
                <tr>
                  <th>第几次</th>
                  <th>状态</th>
                  <th>耗时 (s)</th>
                  <th>天数</th>
                  <th>prompt token</th>
                  <th>completion token</th>
                  <th>Planner 输出</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="run in data.timing.rows" :key="run.run">
                  <td>{{ run.run }}</td>
                  <td>{{ run.status }}</td>
                  <td>{{ fmt(run.seconds) }}</td>
                  <td>{{ fmt(run.days) }}</td>
                  <td>{{ fmt(run.prompt_tokens) }}</td>
                  <td>{{ fmt(run.completion_tokens) }}</td>
                  <td>{{ fmt(run.planner_out) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </div>

      <!-- ③ 已保存行程 token 统计 -->
      <div class="ios-card">
        <div class="section-head">
          <div>
            <h3 class="section-title">已保存行程 token 统计</h3>
            <p class="section-sub">等价于 /trip/stats，实时读取本地数据库</p>
          </div>
          <span v-if="!data.trip_stats.available" class="tag tag--missing">数据缺失</span>
        </div>

        <div v-if="!data.trip_stats.available" class="section-empty">
          {{ data.trip_stats.note || "行程 token 统计不可用。" }}
        </div>
        <div v-else class="stat-grid stat-grid--four">
          <div class="stat-box">
            <div class="stat-box__label">行程数</div>
            <div class="stat-box__value">{{ fmt(data.trip_stats.trip_count) }}</div>
          </div>
          <div class="stat-box">
            <div class="stat-box__label">输入 token</div>
            <div class="stat-box__value">{{ fmt(data.trip_stats.total_prompt_tokens) }}</div>
          </div>
          <div class="stat-box">
            <div class="stat-box__label">输出 token</div>
            <div class="stat-box__value">{{ fmt(data.trip_stats.total_completion_tokens) }}</div>
          </div>
          <div class="stat-box">
            <div class="stat-box__label">总 token</div>
            <div class="stat-box__value">{{ fmt(data.trip_stats.total_tokens) }}</div>
          </div>
        </div>
      </div>

      <!-- ④ 数据来源 -->
      <div class="ios-card">
        <h3 class="section-title">数据来源</h3>
        <p class="section-sub">
          快照来自仓库内静态数据，不是实时测量；缺失项会明确标注，不会用旧数字假装稳定。
        </p>
        <div class="source-grid">
          <div
            v-for="source in data.sources"
            :key="source.name"
            :class="['source-card', { 'source-card--missing': !source.available }]"
          >
            <div class="source-card__head">
              <span class="source-card__name">{{ SOURCE_TEXT[source.name] ?? source.name }}</span>
              <span :class="['tag', `tag--${source.kind}`]">{{ KIND_TEXT[source.kind] }}</span>
            </div>
            <code class="source-card__loc">{{ source.location }}</code>
            <p class="source-card__note">{{ source.note }}</p>
          </div>
        </div>
      </div>

      <!-- ⑤ 口径提醒 -->
      <div class="ios-card notes-card">
        <h3 class="section-title">口径提醒</h3>
        <ul class="notes-list">
          <li v-for="(note, index) in data.notes" :key="index">{{ note }}</li>
        </ul>
      </div>
    </template>
  </section>
</template>

<style scoped>
.metrics-page {
  display: grid;
  gap: 12px;
}

.ios-card {
  padding: 20px;
  border-radius: 12px;
  background: #FFFFFF;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

.ios-empty {
  text-align: center;
  color: #8E8E93;
  font-size: 14px;
  padding: 40px 20px;
}

.ios-empty--error { color: #FF3B30; }
.ios-empty p { margin: 0 0 12px; }

.ios-btn {
  border: none;
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;
}
.ios-btn:active { transform: scale(0.97); }
.ios-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.ios-btn--primary { background: #007AFF; color: #FFFFFF; }
.ios-btn--sm { padding: 6px 14px; font-size: 13px; }

/* 头部 */
.metrics-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.metrics-header__title {
  margin: 0 0 4px;
  font-size: 22px;
  font-weight: 700;
  color: #1C1C1E;
}
.metrics-header__desc {
  margin: 0;
  font-size: 14px;
  color: #8E8E93;
}
.metrics-header__actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.status-badge {
  padding: 5px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}
.status-badge--ok { background: rgba(52, 199, 89, 0.12); color: #248A3D; }
.status-badge--partial { background: rgba(255, 149, 0, 0.14); color: #C77700; }
.status-badge--empty { background: rgba(142, 142, 147, 0.16); color: #636366; }

/* 段落 */
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 14px;
}
.section-title {
  margin: 0;
  font-size: 17px;
  font-weight: 600;
  color: #1C1C1E;
}
.section-sub {
  margin: 4px 0 0;
  font-size: 12px;
  color: #8E8E93;
  line-height: 1.6;
}
.section-sub--foot { margin-top: 10px; }
.section-empty {
  padding: 24px 16px;
  border-radius: 8px;
  background: #F2F2F7;
  color: #636366;
  font-size: 13px;
  line-height: 1.6;
}

.tag {
  display: inline-block;
  flex-shrink: 0;
  padding: 3px 9px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  background: rgba(0, 122, 255, 0.10);
  color: #007AFF;
}
.tag--live { background: rgba(52, 199, 89, 0.12); color: #248A3D; }
.tag--missing { background: rgba(255, 59, 48, 0.10); color: #D70015; }

/* 表格 */
.table-scroll { overflow-x: auto; }
.metrics-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  min-width: 720px;
}
.metrics-table th,
.metrics-table td {
  padding: 10px 12px;
  text-align: right;
  border-bottom: 0.5px solid rgba(0, 0, 0, 0.07);
  white-space: nowrap;
}
.metrics-table th {
  font-weight: 600;
  font-size: 12px;
  color: #8E8E93;
  background: #F9F9FB;
}
.metrics-table th:first-child,
.metrics-table td:first-child { text-align: left; }
.metrics-table tbody tr:last-child td { border-bottom: none; }
.metrics-table td { color: #3C3C43; font-variant-numeric: tabular-nums; }
.cell-mode { font-weight: 600; color: #1C1C1E !important; }

/* token 构成条 */
.bars {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 0.5px solid rgba(0, 0, 0, 0.07);
}
.bars__head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.bars__title { font-size: 13px; font-weight: 600; color: #3C3C43; }
.bars__hint { font-size: 11px; color: #8E8E93; }
.bar-row {
  display: grid;
  grid-template-columns: 190px 1fr 80px;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.bar-row__label { font-size: 12px; color: #636366; }
.bar-row__track {
  display: flex;
  height: 14px;
  border-radius: 7px;
  overflow: hidden;
  background: #F2F2F7;
}
.bar-seg { height: 100%; transition: width 0.3s ease; }
.bar-row__total {
  text-align: right;
  font-size: 12px;
  color: #3C3C43;
  font-variant-numeric: tabular-nums;
}
.bars__legend {
  display: flex;
  gap: 16px;
  margin-top: 10px;
  font-size: 11px;
  color: #8E8E93;
}
.bars__legend span { display: inline-flex; align-items: center; gap: 5px; }
.bars__legend i {
  display: inline-block;
  width: 9px;
  height: 9px;
  border-radius: 2px;
}

/* 数字卡 */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}
.stat-grid--four { grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: 0; }
.stat-box {
  padding: 14px;
  border-radius: 10px;
  background: #F2F2F7;
}
.stat-box__label { font-size: 12px; color: #8E8E93; }
.stat-box__value {
  margin-top: 6px;
  font-size: 24px;
  font-weight: 700;
  color: #1C1C1E;
  font-variant-numeric: tabular-nums;
}
.stat-box__unit { font-size: 13px; font-weight: 500; color: #8E8E93; }
.stat-box__note { margin-top: 4px; font-size: 11px; color: #8E8E93; }

/* 来源 */
.source-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 10px;
  margin-top: 14px;
}
.source-card {
  padding: 12px;
  border-radius: 10px;
  background: #F9F9FB;
  border: 0.5px solid rgba(0, 0, 0, 0.06);
}
.source-card--missing { background: rgba(255, 59, 48, 0.04); }
.source-card__head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.source-card__name { font-size: 13px; font-weight: 600; color: #1C1C1E; }
.source-card__loc {
  display: block;
  margin-top: 8px;
  font-size: 11px;
  color: #636366;
  word-break: break-all;
}
.source-card__note {
  margin: 6px 0 0;
  font-size: 11px;
  color: #8E8E93;
  line-height: 1.5;
}

/* 口径提醒 */
.notes-card { background: #FFFDF5; }
.notes-list {
  margin: 12px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.8;
  color: #636366;
}

@media (max-width: 768px) {
  .metrics-header { flex-direction: column; align-items: flex-start; }
  .bar-row { grid-template-columns: 130px 1fr 60px; }
  .stat-grid--four { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
