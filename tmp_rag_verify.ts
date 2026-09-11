// Verify the Onyx /rag page: iframe loads the LightRAG embed build and
// backend calls route through the onyx rewrites.
import { chromium } from "/tmp/node_modules/playwright-core/index.js";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const BASE = "http://localhost:3001";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });

  const requests: string[] = [];
  page.on("request", (req) => {
    const u = req.url();
    if (!u.includes("localhost:3001/_next") && !u.includes(".js") && !u.includes(".css")) {
      requests.push(`${req.method()} ${u.replace(BASE, "")}`);
    }
  });
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (err) => errors.push("PAGEERROR: " + err.message));

  // 1. Top-level /rag page
  await page.goto(`${BASE}/rag?doc=HP-1&connector=JIRA&connectorName=jira-connector2`, {
    waitUntil: "domcontentloaded",
    timeout: 60000,
  });
  await sleep(4000);

  const iframeCount = await page.locator("iframe").count();
  console.log("TOP-LEVEL iframes:", iframeCount);

  // 2. Inside the iframe: wait for the embed app to render
  const frame = page.frames().find((f) => f.url().includes("/lightrag-ui/"));
  if (!frame) {
    console.log("FRAME NOT FOUND. Frames:");
    for (const f of page.frames()) console.log("  -", f.url());
    await page.screenshot({ path: "/tmp/rag_verify_top.png" }).catch(() => {});
    console.log("TOP-LEVEL console errors:", errors.slice(0, 8));
    await browser.close();
    return;
  }
  console.log("IFRAME URL:", frame.url());

  await sleep(10000); // guest login + graph fetch

  const headers = await frame.locator("header").count().catch(() => -1);
  const canvas = await frame.locator("canvas").count().catch(() => -1);
  const tabs = await frame.locator('[role="tab"]').count().catch(() => -1);
  console.log("IFRAME header count:", headers, "(expect 0 in embed mode)");
  console.log("IFRAME canvas count:", canvas, "(expect >0: graph rendered)");
  console.log("IFRAME tab count:", tabs, "(expect 0 in embed mode)");

  await page.screenshot({ path: "/tmp/rag_verify_iframe.png" }).catch(() => {});

  // 3. Request audit
  const interesting = requests.filter((r) =>
    /lightrag-api|magicbox|\/onyx|subgraph|auth|graphs/.test(r)
  );
  console.log("\n=== KEY REQUESTS ===");
  for (const r of interesting) console.log(" ", r);

  const badStatus = requests.filter((r) => r.includes("FAILED"));
  console.log("\n=== FAILED REQUESTS ===", badStatus.length);
  console.log("\n=== CONSOLE ERRORS ===");
  console.log(errors.slice(0, 12).join("\n") || "(none)");

  await browser.close();
}

main().catch((e) => {
  console.error("FATAL:", e.message);
});
