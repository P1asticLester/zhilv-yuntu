import { expect, test } from "@playwright/test";

import { PlanPage } from "./pages/plan.page";

/**
 * 10 条打桩 UI 冒烟：等价类 / 边界值 / 错误注入的设计依据见 ./CASES.md。
 *
 * 刻意只放在测试金字塔最顶端：预算算法、校验、降级分支等业务逻辑由
 * backend/tests/ 的 131 条契约测试覆盖；这里只验前端交互、渲染与错误态。
 * 请求全部打桩，不依赖模型 / 高德 / 网络，不进 pytest 收集与 CI 默认门禁。
 */
test.describe("UI 冒烟 · 等价类 / 边界值 / 错误态（后端请求已打桩）", () => {
  test("① 首页加载：标题、默认目的地与提交按钮可用", async ({ page }) => {
    const plan = new PlanPage(page);
    await plan.mockBackend();
    await plan.goto();
    await plan.expectHomeReady();
  });

  test("② 6 城目的地：提交后切到结果页并渲染行程", async ({ page }) => {
    const plan = new PlanPage(page);
    await plan.mockBackend();
    await plan.goto();
    await plan.plan("大理");

    await expect(plan.resultHeading()).toContainText("大理旅行计划");
    await expect(page.locator(".ios-info").first()).toContainText("行程 ID");
    await expect(plan.weatherHeader()).toBeVisible();
    await expect(plan.resultPage()).toBeVisible();
  });

  test("③ 晴天分支：保留防晒 tips，不注入雨提示", async ({ page }) => {
    const plan = new PlanPage(page);
    await plan.mockBackend({ rainy: false });
    await plan.goto();
    await plan.plan("大理");

    await expect(plan.tips()).toContainText("注意防晒");
    await expect(plan.tips()).not.toContainText("天气可能有雨");
  });

  test("④ 雨天分支：注入两条雨提示并过滤防晒", async ({ page }) => {
    const plan = new PlanPage(page);
    await plan.mockBackend({ rainy: true });
    await plan.goto();
    await plan.plan("大理");

    await expect(plan.tips()).toContainText("天气可能有雨，建议随身带伞或轻便雨衣。");
    await expect(plan.tips()).toContainText("阴雨天路面湿滑，建议穿防滑鞋。");
    // Result.vue 的 sunnyTipKeywords 会在雨天过滤掉含「防晒」的提示。
    await expect(plan.tips()).not.toContainText("注意防晒");
  });

  // 目的地等价类：6 城（正常）/ 省级 / 空值（后两者由后端 422 拒绝）。
  for (const { label, city } of [
    { label: "⑤ 省级目的地「青海」", city: "青海" },
    { label: "⑥ 空目的地", city: "" },
  ]) {
    test(`${label}：后端 422，停留首页并展示错误提示`, async ({ page }) => {
      const plan = new PlanPage(page);
      await plan.mockBackend({ mode: "invalid_destination" });
      await plan.goto();
      await plan.plan(city);

      await plan.expectErrorToast("请输入具体城市");
      await plan.expectStayOnHome();
    });
  }

  test("⑦ 边界值参数化：5 组输入原样透传，后端 422 时停留首页", async ({ page }) => {
    const inputs = [
      { label: "日期倒挂", start: "2026-12-10", end: "2026-12-01" },
      { label: "人数 0（min=1 下界外）", travelers: "0" },
      { label: "人数 -1（负数）", travelers: "-1" },
      { label: "预算 -1（min=0 下界外）", budget: "-1" },
      { label: "预算极大值", budget: "999999999" },
    ];
    const seen: Record<string, unknown>[] = [];
    const plan = new PlanPage(page);
    // 只注册一次 route，5 组请求都被同一 handler 收集，避免路由叠加。
    await plan.mockBackend({
      mode: "invalid_destination",
      onRequest: (payload) => seen.push(payload),
    });
    await plan.goto();

    for (const input of inputs) {
      await test.step(input.label, async () => {
        await plan.fillAndSubmit(input);
        await plan.expectErrorToast("请输入具体城市");
        await plan.expectStayOnHome();
      });
    }

    expect(seen).toHaveLength(inputs.length);
    expect(seen[0].start_date).toBe("2026-12-10");
    expect(seen[0].end_date).toBe("2026-12-01");
    expect(seen[1].travelers).toBe(0);
    expect(seen[2].travelers).toBe(-1);
    expect(seen[3].budget).toBe(-1);
    expect(seen[4].budget).toBe(999999999);
  });

  test("⑧ 提交中间态：按钮进入「正在生成中...」且不可重复点击", async ({ page }) => {
    const plan = new PlanPage(page);
    await plan.mockBackend({ delayMs: 1200 });
    await plan.goto();
    await plan.plan("大理");

    await plan.expectSubmitting();
    await expect(plan.resultHeading()).toContainText("大理旅行计划");
  });

  test("⑨ 后端 502：展示错误提示、不切结果页", async ({ page }) => {
    const plan = new PlanPage(page);
    await plan.mockBackend({ mode: "server_error" });
    await plan.goto();
    await plan.plan("大理");

    await plan.expectErrorToast("上游服务不可用");
    await plan.expectStayOnHome();
  });

  test("⑩ 结果页「← 返回规划」回到首页", async ({ page }) => {
    const plan = new PlanPage(page);
    await plan.mockBackend();
    await plan.goto();
    await plan.plan("大理");
    await expect(plan.resultPage()).toBeVisible();

    await plan.goBackToPlan();
    await expect(plan.resultPage()).toHaveCount(0);
  });
});
