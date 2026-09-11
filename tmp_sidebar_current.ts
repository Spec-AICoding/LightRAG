// Check what the CURRENT dev server (PID changed again) actually renders.
import { chromium } from "/tmp/pw/node_modules/playwright-core/index.js";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const BASE = "http://localhost:3001";
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errs: string[] = [];
  page.on("pageerror", (e) => errs.push("PAGEERROR: " + e.message));
  page.on("console", (m) => m.type() === "error" && errs.push(m.text()));

  await page.goto(`${BASE}/auth/login`, { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(8000);
  console.log("LOGIN URL:", page.url());
  const inputs = page.locator("input");
  const n = await inputs.count();
  console.log("INPUTS:", n);
  if (n < 2) {
    console.log("BODY:", JSON.stringify(await page.evaluate(() => document.body.innerText.slice(0, 300))));
    await browser.close();
    return;
  }
  await inputs.nth(0).fill("wpf0310@163.com");
  await inputs.nth(1).fill("MagicBox123!");
  await page.locator("button[type=submit]").first().click();
  await sleep(12000);
  console.log("AFTER LOGIN URL:", page.url());

  const text = await page.evaluate(() => document.body.innerText);
  console.log("HAS 'Knowledge Governance':", text.includes("Knowledge Governance"));
  console.log("HAS 'Knowledge Graph':", text.includes("Knowledge Graph"));
  console.log("HAS 'Admin Panel':", text.includes("Admin Panel"));
  console.log("HAS 'Recents':", text.includes("Recents"));
  console.log("HAS 'New Session':", text.includes("New Session"));

  // Also verify which chunk is loaded — check for the new code marker
  const chunkCheck = await page.evaluate(() => {
    // find the AppSidebar module by looking at rendered sidebar DOM
    const aside = document.querySelector("aside, nav");
    const hasSection = !!document.querySelector("[class*=sidebar-section]");
    return { hasAside: !!aside, hasSection };
  });
  console.log("DOM CHECK:", JSON.stringify(chunkCheck));

  console.log("ERRORS:", errs.slice(0, 8).join("\n") || "(none)");
  await page.screenshot({ path: "/tmp/sidebar_current.png" }).catch(() => {});
  await browser.close();
}

main().catch((e) => console.error("FATAL:", e.message));
