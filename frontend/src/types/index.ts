export interface TripRequestPayload {
  destination: string;
  start_date: string;
  end_date: string;
  travelers: number;
  budget: number;
  preferences: string[];
  pace?: string | null;
  dietary_preferences: string[];
  hotel_level?: string | null;
  special_notes?: string | null;
}

export interface TripEditPayload {
  trip_id: string;
  current_itinerary: Itinerary;
  user_instruction: string;
  edit_scope?: string | null;
  preserve_constraints: string[];
}

export interface SpotItem {
  name: string;
  start_time?: string | null;
  end_time?: string | null;
  description?: string | null;
  estimated_cost?: number;
  location?: string | null;
  image_url?: string | null;
  address?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  poi_id?: string | null;
}

export interface MealItem {
  name: string;
  meal_type: string;
  estimated_cost?: number;
  notes?: string | null;
  address?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  poi_id?: string | null;
  image_url?: string | null;
}

export interface HotelItem {
  name: string;
  level?: string | null;
  estimated_cost?: number;
  location?: string | null;
  address?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  poi_id?: string | null;
  image_url?: string | null;
}

export interface TransportItem {
  mode: string;
  from_place?: string | null;
  to_place?: string | null;
  estimated_cost?: number;
  duration?: string | null;
  distance_km?: number | null;
  estimated_minutes?: number | null;
}

export interface DayPlan {
  day_index: number;
  date?: string | null;
  theme?: string | null;
  spots: SpotItem[];
  meals: MealItem[];
  hotel?: HotelItem | null;
  transport: TransportItem[];
  notes: string[];
}

export interface BudgetBreakdown {
  transport: number;
  hotel: number;
  meals: number;
  tickets: number;
  other: number;
  total: number;
}

export interface Itinerary {
  trip_id: string;
  destination: string;
  summary: string;
  days: DayPlan[];
  estimated_budget: number;
  budget_breakdown: BudgetBreakdown;
  tips: string[];
  source_notes: string[];
}

export interface TripSaveResponse {
  message: string;
  trip_id: string;
}

export interface TripSummaryItem {
  trip_id: string;
  destination: string;
  summary: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface TripListResponse {
  total: number;
  items: TripSummaryItem[];
}

export interface TripDetailResponse {
  trip_id: string;
  itinerary: Itinerary;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface WeatherForecastDay {
  date?: string | null;
  week?: string | null;
  day_weather?: string | null;
  night_weather?: string | null;
  day_temp?: string | null;
  night_temp?: string | null;
  day_wind?: string | null;
  night_wind?: string | null;
}

export interface WeatherForecastResponse {
  city: string;
  province?: string | null;
  adcode?: string | null;
  report_time?: string | null;
  days: WeatherForecastDay[];
}

/* ------------------------------------------------------------------ *
 * 6b · 成本看板：与后端 GET /metrics/dashboard（6a）的 Pydantic 契约一一对应。
 * 字段名、可空性均照抄 backend/app/models/metrics_schemas.py，不做换算。
 * 三段数据都可能缺失，所以每段都有 available + note，顶层有 status。
 * ------------------------------------------------------------------ */

export type MetricsDashboardStatus = "ok" | "partial" | "empty";
export type MetricsSourceKind = "snapshot" | "live" | "missing";

/** ablation_summary.csv 的一行；avg_latency_ms 只量检索阶段，不是端到端。 */
export interface AblationRow {
  mode: string;
  rounds_ok: number;
  redis_enabled: boolean;
  cases: number;
  top1_pct: number;
  topk_pct: number;
  mrr: number;
  avg_latency_ms: number;
  embedding_tokens: number;
  rerank_tokens: number;
  rewrite_prompt_tokens: number;
  rewrite_completion_tokens: number;
}

/** timing_result.csv 的一行；seconds 才是端到端 /trip/generate 耗时。 */
export interface TimingRow {
  run: number;
  status: number;
  seconds: number;
  trip_id: string;
  days: number;
  prompt_tokens: number;
  completion_tokens: number;
  planner_out: number;
  error: string;
}

export interface DataSourceInfo {
  name: string;
  location: string;
  available: boolean;
  kind: MetricsSourceKind;
  note: string;
}

export interface AblationSection {
  available: boolean;
  rows: AblationRow[];
  note: string;
}

export interface TimingSection {
  available: boolean;
  rows: TimingRow[];
  median_seconds: number | null;
  note: string;
}

/** 等价于 GET /trip/stats 的聚合值。 */
export interface TripStatsSection {
  available: boolean;
  trip_count: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
  item_count: number;
  note: string;
}

export interface MetricsDashboardResponse {
  status: MetricsDashboardStatus;
  sources: DataSourceInfo[];
  ablation: AblationSection;
  timing: TimingSection;
  trip_stats: TripStatsSection;
  notes: string[];
}
