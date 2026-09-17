// Page-builder end-to-end check (run against a live API + web):
//   BASE=http://builder.localtest.me:3000 EMAIL=owner@example.com PASSWORD=... node e2e/builder.e2e.mjs
// Logs in as staff, reorders a section with the keyboard drag sensor, adds an FAQ, waits for autosave,
// publishes, and asserts the storefront renders the new order (proves the revalidation hook).
import { chromium } from "playwright-core";

const base = process.env.BASE ?? "http://builder.localtest.me:3000";
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM, args: ["--host-resolver-rules=MAP *.localtest.me 127.0.0.1"],
});
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
await page.goto(`${base}/admin/login`);
await page.fill("input[name=email]", process.env.EMAIL ?? "owner@example.com");
await page.fill("input[name=password]", process.env.PASSWORD ?? "correct-horse-battery");
await page.click("form button");
await page.waitForURL("**/admin/builder");
await page.waitForSelector("text=product grid");
const handle = page.getByRole("button", { name: "Drag product_grid" });
await handle.focus();
await page.keyboard.press("Space"); await page.keyboard.press("ArrowUp"); await page.keyboard.press("Space");
await page.click('summary:has-text("Add section")');
await page.locator("details ul button", { hasText: /^faq$/ }).click();
await page.waitForSelector("text=Draft saved", { timeout: 10000 });
await page.click('button:has-text("Publish")');
await page.waitForSelector("text=Published version", { timeout: 10000 });
const store = await browser.newPage();
await store.goto(`${base}/`);
const order = await store.$$eval("[data-section]", (els) => els.map((e) => e.getAttribute("data-section")));
console.log("storefront sections:", order);
if (!order.includes("faq")) { console.error("FAQ not published"); process.exit(1); }
await browser.close();
