// Diagnose what the top-level /rag page actually renders when not logged in.
import { chromium } from "/tmp/node_modules/playwright-core/index.js";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors: string[] = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message));

  await page.goto("http://localhost:3001/rag", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(8000);
  console.log("FINAL URL:", page.url());
  const body = await page.evaluate(() => document.body.innerText.slice(0, 400));
  console.log("BODY TEXT:", JSON.stringify(body));
  const html = await page.evaluate(() => document.body.innerHTML.slice(0, 300));
  console.log("BODY HTML HEAD:", html);
  console.log("ERRORS:", errors.slice(0, 5));
  await browser.close();
}

main().catch((e) => console.error("FATAL:", e.message));
