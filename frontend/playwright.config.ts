import { defineConfig } from "@playwright/test";

/**
 * 方案 A · 前端独立 e2e runner（Step 4）
 *
 * - 只在本地 `npm run e2e` 时运行；不进 backend/tests/ 的 pytest 收集，
 *   也不进 .github/workflows/test.yml 的 CI 默认门禁（那个门禁必须保持
 *   「断网无密钥 131 passed」）。
 * - webServer 自己起 Vite。后端按现有方式单独起；本套 3 条冒烟用
 *   page.route() 把 /trip/generate 与 /weather/forecast 挡成固定响应，
 *   所以不需要后端在线、不需要真实 API Key，也不属于 Step 5 的在线评估。
 * - 用系统已装的 Edge（channel: "msedge"），避免 `npx playwright install`
 *   下载几百 MB 浏览器。若改用自带 Chromium，注释掉 channel 并先装浏览器。
 * - 失败时留 trace.zip / 截图到 test-results/，均被 .gitignore 忽略。
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  retries: 0,
  reporter: "list",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: "http://127.0.0.1:5173",
    channel: "msedge",
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5173",
    url: "http://127.0.0.1:5173",
    // 这套测试不进 CI，本地有现成 Vite 就直接复用，省一次冷启动。
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
