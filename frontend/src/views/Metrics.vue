<script setup lang="ts">
/**
 * 成本页（只读，面向游客）。
 *
 * 数据源：GET /metrics/dashboard，契约见 types/index.ts 的 MetricsDashboardResponse。
 * 开发者排查信息（数据来源、文件路径、接口名、环境变量、逐次请求明细）不进界面，
 * 需要时看后端接口与 docs/ 下的度量产物。
 *
 * 口径约束（保留在界面上的部分，删了会误读数字）：
 * - 三段数据都可能缺失，available=false 时显示空状态与 note，不拿 0 冒充真实数据；
 * - 样本数与取中位数的方式必须标注（CHANGELOG 的 80%→93.3% 基于 15 条样例时期）；
 * - 数字原样展示，不派生「快了 N%」「省了多少」这类结论；
 * - 用量条只表示同一方案内各环节的占比，不是节省量、不是优化收益；
 * - 质量与成本同屏，避免只看成本得出「关掉某个环节更好」。
 */
import { computed, onMounted, ref, watch } from "vue";

import { fetchMetricsDashboard } from "../services/api";
import type {
  AblationRow,
  MetricsDashboardResponse,
  MetricsDashboardStatus,
} from "../types";

const props = defineProps<{
  active: boolean;
}>();

const loading = ref(false);
const error = ref("");
const data = ref<MetricsDashboardResponse | null>(null);

const MODE_LABEL: Record<string, string> = {
  base: "完整方案",
  norew: "不做改写",
  norr: "不做精排",
};

const STATUS_TEXT: Record<MetricsDashboardStatus, string> = {
  ok: "数据齐全",
  partial: "部分可用",
  empty: "暂无数据",
};

function modeLabel(mode: string): string {
  return MODE_LABEL[mode] ?? mode;
}

function fmt(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  // 至少保留 2 位：文档中端到端中位数 28.82s 等是两位小数的既定口径。
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

/** 同一方案内三个环节的用量占比，仅用于构成展示，不做方案间收益比较。 */
function promptParts(row: AblationRow) {
  const parts = [
    { key: "embedding", label: "检索", value: row.embedding_tokens, color: "#0E6E6A" },
    { key: "rerank", label: "精排", value: row.rerank_tokens, color: "#3FB3A6" },
    { key: "rewrite", label: "改写", value: row.rewrite_prompt_tokens, color: "#B9E6DC" },
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
    error.value = "数据加载失败，请稍后重试。";
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
      <h2 class="metrics-header__title">运行成本</h2>
      <div class="metrics-header__actions">
        <span v-if="data" class="status-badge">{{ STATUS_TEXT[data.status] }}</span>
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
    <div v-else-if="error" class="ios-card ios-empty">
      <p>{{ error }}</p>
      <button class="ios-btn ios-btn--primary ios-btn--sm" @click="loadDashboard">重试</button>
    </div>
    <div v-else-if="!data || !hasAnyData" class="ios-card ios-empty">暂无可用数据</div>

    <template v-else-if="data">
      <!-- ① 不同方案对比 -->
      <div class="ios-card">
        <div class="section-head">
          <div>
            <h3 class="section-title">不同方案对比</h3>
            <p class="section-sub">18 条样例，3 轮取中位数</p>
          </div>
          <span v-if="!data.ablation.available" class="tag">数据缺失</span>
        </div>

        <div v-if="!data.ablation.available" class="section-empty">
          {{ data.ablation.note || "暂无对比数据" }}
        </div>
        <template v-else>
          <div class="table-scroll">
            <table class="metrics-table">
              <thead>
                <tr>
                  <th>方案</th>
                  <th>成功次数</th>
                  <th>首选命中</th>
                  <th>相关命中</th>
                  <th>排序得分</th>
                  <th>检索耗时 (毫秒)</th>
                  <th>精排用量</th>
                  <th>改写用量</th>
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
          <p class="section-sub section-sub--foot">检索耗时不含生成行程的时间</p>

          <!-- 用量构成：同一方案内占比，不是节省量 -->
          <div class="bars">
            <div class="bars__head">
              <span class="bars__title">各环节用量占比</span>
              <span class="bars__hint">仅表示同一方案内的占比</span>
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
              <span><i style="background:#0E6E6A" />检索</span>
              <span><i style="background:#3FB3A6" />精排</span>
              <span><i style="background:#B9E6DC" />改写</span>
            </div>
          </div>
        </template>
      </div>

      <!-- ② 生成耗时 -->
      <div class="ios-card">
        <div class="section-head">
          <div>
            <h3 class="section-title">生成一次行程的耗时</h3>
            <p class="section-sub">3 次实测</p>
          </div>
          <span v-if="!data.timing.available" class="tag">数据缺失</span>
        </div>

        <div v-if="!data.timing.available" class="section-empty">
          {{ data.timing.note || "暂无耗时数据" }}
        </div>
        <div v-else class="stat-grid">
          <div class="stat-box">
            <div class="stat-box__label">耗时中位数</div>
            <div class="stat-box__value">
              {{ data.timing.median_seconds === null ? "—" : fmt(data.timing.median_seconds) }}
              <span class="stat-box__unit">秒</span>
            </div>
            <div class="stat-box__note">首次偏慢为冷启动</div>
          </div>
          <div class="stat-box">
            <div class="stat-box__label">成功次数</div>
            <div class="stat-box__value">
              {{ data.timing.rows.filter((r) => r.status === 200 && !r.error).length }}
              <span class="stat-box__unit">/ {{ data.timing.rows.length }}</span>
            </div>
            <div class="stat-box__note">请求成功且无报错</div>
          </div>
        </div>
      </div>

      <!-- ③ 已保存行程用量 -->
      <div class="ios-card">
        <div class="section-head">
          <div>
            <h3 class="section-title">已保存行程用量</h3>
          </div>
          <span v-if="!data.trip_stats.available" class="tag">数据缺失</span>
        </div>

        <div v-if="!data.trip_stats.available" class="section-empty">
          {{ data.trip_stats.note || "暂无行程用量数据" }}
        </div>
        <div v-else class="stat-grid stat-grid--four">
          <div class="stat-box">
            <div class="stat-box__label">行程数</div>
            <div class="stat-box__value">{{ fmt(data.trip_stats.trip_count) }}</div>
          </div>
          <div class="stat-box">
            <div class="stat-box__label">输入用量</div>
            <div class="stat-box__value">{{ fmt(data.trip_stats.total_prompt_tokens) }}</div>
          </div>
          <div class="stat-box">
            <div class="stat-box__label">输出用量</div>
            <div class="stat-box__value">{{ fmt(data.trip_stats.total_completion_tokens) }}</div>
          </div>
          <div class="stat-box">
            <div class="stat-box__label">合计用量</div>
            <div class="stat-box__value">{{ fmt(data.trip_stats.total_tokens) }}</div>
          </div>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.metrics-page {
  display: grid;
  gap: 12px;
}

/* 宽表格只在自己的容器里横向滚动，不能把整个网格列撑开 */
.metrics-page > * {
  min-width: 0;
}

.ios-card {
  padding: 20px;
  border-radius: 12px;
  background: #FFFFFF;
}

.ios-empty {
  text-align: center;
  color: #8E8E93;
  font-size: 14px;
  padding: 40px 20px;
}
.ios-empty p { margin: 0 0 12px; }

.ios-btn {
  border: none;
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}
.ios-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.ios-btn--primary { background: #0E9A93; color: #FFFFFF; }
.ios-btn--sm { padding: 6px 14px; font-size: 14px; }

/* 头部 */
.metrics-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.metrics-header__title {
  margin: 0;
  font-size: 22px;
  font-weight: 700;
  color: #1C1C1E;
}
.metrics-header__actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.status-badge {
  padding: 5px 10px;
  border: 1px solid currentColor;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  color: #636366;
}

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
  font-size: 14px;
  line-height: 1.6;
}
.tag {
  display: inline-block;
  flex-shrink: 0;
  padding: 3px 9px;
  border: 1px solid currentColor;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  color: #636366;
}

/* 表格 */
.table-scroll { overflow-x: auto; }
.metrics-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
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

/* 用量构成条 */
.bars {
  margin-top: 18px;
  padding-top: 16px;
}
.bars__head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.bars__title { font-size: 14px; font-weight: 600; color: #3C3C43; }
.bars__hint { font-size: 12px; color: #8E8E93; }
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
.bar-seg { height: 100%; }
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
  font-size: 12px;
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
}
.stat-grid--four { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.stat-box {
  padding: 14px;
  border-radius: 10px;
  background: #F2F2F7;
}
.stat-box__label { font-size: 12px; color: #8E8E93; }
.stat-box__value {
  margin-top: 6px;
  font-size: 22px;
  font-weight: 700;
  color: #1C1C1E;
  font-variant-numeric: tabular-nums;
}
.stat-box__unit { font-size: 14px; font-weight: 500; color: #8E8E93; }
.stat-box__note { margin-top: 4px; font-size: 12px; color: #8E8E93; }

@media (max-width: 768px) {
  .metrics-header { flex-direction: column; align-items: flex-start; }
  .bar-row { grid-template-columns: 130px 1fr 60px; }
  .stat-grid--four { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
