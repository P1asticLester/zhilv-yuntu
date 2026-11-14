import { expect, test } from "@playwright/test";

import { PlanPage } from "./pages/plan.page";

/**
 * 3 条核心旅程冒烟。刻意只放金字塔最顶端：
 * 表单校验、预算算法、降级分支等业务逻辑由 backend/tests/ 覆盖，
 * 这里只回答「页面能不能打开、点了能不能到结果页、天气分支会不会改 tips」。
 */
test.describe("核心旅程 UI 冒烟（后端请求已打桩）", () => {
  test("首页加载：标题、默认目的地与提交按钮可见", async ({ page }) => {
    const plan = new PlanPage(page);
    await plan.mockBackend();
    await plan.goto();
    await plan.expectHomeReady();
  });

  test("提交后切换到结果页并渲染行程与晴天提示", async ({ page }) => {
    const plan = new PlanPage(page);
    await plan.mockBackend();
    await plan.goto();
    await plan.plan("大理");

    await expect(plan.resultHeading()).toContainText("大理旅行计划");
    await expect(page.locator(".ios-info").first()).toContainText("行程 ID");
    await expect(plan.weatherHeader()).toBeVisible();
    await expect(plan.tips()).toContainText("注意防晒");
    await expect(plan.tips()).not.toContainText("天气可能有雨");
  });

  test("雨天分支：tips 增雨提示并过滤晴天提示", async ({ page }) => {
    const plan = new PlanPage(page);
    await plan.mockBackend({ rainy: true });
    await plan.goto();
    await plan.plan("大理");

    await expect(plan.resultHeading()).toContainText("大理旅行计划");
    await expect(plan.tips()).toContainText("天气可能有雨，建议随身带伞或轻便雨衣。");
    await expect(plan.tips()).toContainText("阴雨天路面湿滑，建议穿防滑鞋。");
    // Result.vue 的 sunnyTipKeywords 会在雨天过滤掉含「防晒」的提示。
    await expect(plan.tips()).not.toContainText("注意防晒");
  });
});
