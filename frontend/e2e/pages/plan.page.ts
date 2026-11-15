import { expect, type Page, type Route } from "@playwright/test";

/**
 * minimalist Page Object + 固定响应 / 故障注入。
 *
 * UI 冒烟只验「前端交互与渲染」：后端 /trip/generate、/weather/forecast
 * 全部用 page.route() 挡成固定数据，业务正确性由 backend/tests/ 的 131 条
 * 契约测试覆盖。这样 E2E 不依赖模型 / 高德 / 网络，也不会变慢变脆。
 * 等价类 / 边界值的设计依据见 ./CASES.md。
 */

/** 与 frontend/src/types/index.ts 的 Itinerary 形状一致（不含 token_usage）。 */
export const MOCK_ITINERARY = {
  trip_id: "trip_dali_ui_smoke",
  destination: "大理",
  summary: "这是一份用于 UI 冒烟的固定行程响应。",
  days: [
    {
      day_index: 1,
      date: "2026-11-10",
      theme: "古城慢游",
      spots: [
        {
          name: "大理古城",
          description: "固定测试景点",
          estimated_cost: 0,
          location: "大理",
        },
      ],
      meals: [{ name: "古城小吃", meal_type: "午餐", estimated_cost: 60 }],
      hotel: { name: "古城客栈", level: "舒适型", estimated_cost: 300 },
      transport: [{ mode: "步行", estimated_cost: 0 }],
      notes: ["按固定响应渲染"],
    },
  ],
  estimated_budget: 360,
  budget_breakdown: {
    transport: 0,
    hotel: 300,
    meals: 60,
    tickets: 0,
    other: 0,
    total: 360,
  },
  tips: [
    "建议根据大理当天实时天气准备雨具或薄外套。",
    "注意防晒，晴天紫外线较强。",
    "古镇石板路适合慢走，鞋子选舒适防滑款。",
  ],
  source_notes: ["UI 冒烟固定响应，不代表真实模型输出。"],
};

const SUNNY_WEATHER = {
  city: "大理",
  days: [
    {
      date: "2026-11-10",
      week: "2",
      day_weather: "晴",
      night_weather: "多云",
      day_temp: "22",
      night_temp: "12",
    },
  ],
};

const RAINY_WEATHER = {
  city: "大理",
  days: [
    {
      date: "2026-11-10",
      week: "2",
      day_weather: "小雨",
      night_weather: "中雨",
      day_temp: "18",
      night_temp: "13",
    },
  ],
};

// api.ts 的 baseURL 是 http://localhost:8000，与 Vite 的 127.0.0.1:5173
// 不同源；被拦截的响应（含 422 / 502 错误体）也要带 CORS 头，否则 axios
// 在浏览器层拿不到响应，只会报 CORS 错误而不是后端错误。
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "*",
};

/** Home.vue 的 getBackendErrorMessage 能读出 detail.message / 字符串 detail。 */
const INVALID_DESTINATION = {
  detail: { code: "invalid_destination", message: "请输入具体城市" },
};
const SERVER_ERROR = { detail: "上游服务不可用" };

function fulfillJson(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(body),
  });
}

export type BackendMode = "ok" | "invalid_destination" | "server_error";

export interface MockOptions {
  /** 天气分支：true 注入雨天，触发 Result.vue 增删 tips。 */
  rainy?: boolean;
  /** 注入后端的返回类型，用来验前端的错误态处理。 */
  mode?: BackendMode;
  /** 延迟响应，用来捕获「正在生成中...」中间态。 */
  delayMs?: number;
  /** 捕获真实发出的请求体，用来验输入是否原样透传。 */
  onRequest?: (payload: Record<string, unknown>) => void;
}

export class PlanPage {
  constructor(private readonly page: Page) {}

  /** 把后端请求挡成固定响应 / 故障响应；断网、无密钥、无后端也能跑。 */
  async mockBackend(options: MockOptions = {}) {
    const weather = options.rainy ? RAINY_WEATHER : SUNNY_WEATHER;

    await this.page.route("**/trip/generate", async (route) => {
      // axios 发 application/json 会先触发 CORS 预检。
      if (route.request().method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      if (options.onRequest) {
        const raw = route.request().postData();
        options.onRequest(raw ? (JSON.parse(raw) as Record<string, unknown>) : {});
      }
      if (options.delayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.delayMs));
      }
      if (options.mode === "invalid_destination") {
        await fulfillJson(route, INVALID_DESTINATION, 422);
        return;
      }
      if (options.mode === "server_error") {
        await fulfillJson(route, SERVER_ERROR, 502);
        return;
      }
      await fulfillJson(route, MOCK_ITINERARY);
    });
    await this.page.route("**/weather/forecast**", (route) =>
      fulfillJson(route, weather),
    );
    // 本机若配了 VITE_AMAP_JS_KEY，结果页会注入高德 JS；拦掉保证纯离线。
    await this.page.route("https://webapi.amap.com/**", (route) =>
      route.abort(),
    );
  }

  async goto() {
    await this.page.goto("/");
  }

  /** Home.vue 里 .ios-input 的固定顺序，别用易变的中文 label 定位。 */
  field(index: number) {
    // 0 目的地 / 1 开始日期 / 2 结束日期 / 3 人数 / 4 预算
    return this.page.locator(".ios-input").nth(index);
  }

  submitButton() {
    return this.page.getByRole("button", { name: "开始规划" });
  }

  async plan(city = "大理") {
    await this.page.getByPlaceholder("请输入目的地").fill(city);
    await this.submitButton().click();
  }

  /** 按字段名填边界值；Home.vue 没有 JS 校验，输入会原样 POST。 */
  async fillAndSubmit(input: {
    start?: string;
    end?: string;
    travelers?: string;
    budget?: string;
  }) {
    if (input.start !== undefined) await this.field(1).fill(input.start);
    if (input.end !== undefined) await this.field(2).fill(input.end);
    if (input.travelers !== undefined) await this.field(3).fill(input.travelers);
    if (input.budget !== undefined) await this.field(4).fill(input.budget);
    await this.submitButton().click();
  }

  /** 必须限定在 .result-page 内：首页自己也有 3 个 .ios-card__title，
   *  延迟响应时若不限定会命中首页元素并触发 strict mode violation。 */
  resultHeading() {
    return this.page.locator(".result-page .ios-card__title");
  }

  tips() {
    return this.page.locator(".ios-tips");
  }

  weatherHeader() {
    return this.page.locator(".ios-card__header", { hasText: "天气信息" });
  }

  resultPage() {
    return this.page.locator(".result-page");
  }

  errorToast() {
    return this.page.locator(".ant-message");
  }

  async expectHomeReady() {
    await expect(this.page.locator(".nav-bar__title")).toHaveText("智能旅行助手");
    await expect(this.page.getByPlaceholder("请输入目的地")).toHaveValue("大理");
    await expect(this.submitButton()).toBeEnabled();
  }

  /** 失败路径：仍停在首页，没有切到结果页。 */
  async expectStayOnHome() {
    await expect(this.page.getByPlaceholder("请输入目的地")).toBeVisible();
    await expect(this.resultPage()).toHaveCount(0);
  }

  async expectErrorToast(text: string) {
    await expect(this.errorToast()).toContainText(text);
  }

  async expectSubmitting() {
    await expect(
      this.page.getByRole("button", { name: "正在生成中..." }),
    ).toBeDisabled();
  }

  async goBackToPlan() {
    await this.page.getByRole("button", { name: "← 返回规划" }).click();
    await expect(this.page.getByPlaceholder("请输入目的地")).toBeVisible();
  }
}
