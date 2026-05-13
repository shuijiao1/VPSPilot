#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

function arg(name, fallback = '') {
  const i = process.argv.indexOf(`--${name}`);
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  return fallback;
}

(async () => {
  const ip = arg('ip') || process.argv[2];
  if (!ip) throw new Error('Usage: download_ippure.js --ip <IPv4> [--outdir <dir>]');
  const outdir = arg('outdir', '/data/tmp/ippure-downloads');
  fs.mkdirSync(outdir, { recursive: true });

  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({
    acceptDownloads: true,
    viewport: { width: 1440, height: 1200 },
    deviceScaleFactor: 1,
    locale: 'zh-CN',
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  });

  const page = await context.newPage();
  await page.route('**/*', route => {
    const url = route.request().url();
    if (
      url.includes('/cdn-cgi/rum') ||
      url.includes('/cdn-cgi/speculation') ||
      url.includes('cloudflareinsights.com') ||
      url.includes('/api/ads') ||
      url.includes('marker-icon.png') ||
      url.includes('marker-shadow.png')
    ) return route.abort().catch(() => {});
    return route.continue().catch(() => {});
  });

  const url = `https://ippure.com/?ip=${encodeURIComponent(ip)}`;
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.waitForSelector('.iptable-container', { state: 'visible', timeout: 45000 });
  await page.waitForSelector('button.screenshot-btn svg.lucide-camera', { state: 'visible', timeout: 45000 });

  // Faster than waiting for full network idle: wait only for the official card to be populated.
  await page.waitForFunction((targetIp) => {
    const card = document.querySelector('.iptable-container');
    const text = card?.innerText || '';
    return text.includes(targetIp) && text.includes('IPPure系数') && !text.includes('Loading...');
  }, ip, { timeout: 20000 }).catch(() => {});

  // Linux headless Chrome doesn't have PingFang/SF Pro. After Playwright deps are installed,
  // fallback font metrics can make the IPPure score badge wrap (e.g. "40%\n中性").
  // Keep the official camera export path, but stabilize fonts/nowrap inside the exported card.
  await page.addStyleTag({ content: `
    .iptable-container, .iptable-container * {
      font-family: "Noto Sans CJK SC", "Noto Sans SC", "Microsoft YaHei", "PingFang SC", Arial, sans-serif !important;
    }
    .iptable-container .font-mono {
      font-family: "DejaVu Sans Mono", "Noto Sans Mono CJK SC", monospace !important;
    }
    .iptable-container .colormap-indicator-value {
      white-space: nowrap !important;
      min-width: max-content !important;
    }
  ` }).catch(() => {});

  // Wait for web fonts/layout to settle so the export captures the stabilized layout.
  await page.evaluate(async () => {
    await document.fonts?.ready?.catch?.(() => {});
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  }).catch(() => {});

  const cameraButton = page.locator('button.screenshot-btn').filter({ has: page.locator('svg.lucide-camera') }).first();
  const downloadPromise = page.waitForEvent('download', { timeout: 30000 });
  await cameraButton.click({ timeout: 10000 });
  const download = await downloadPromise;
  const suggested = await download.suggestedFilename();
  const filename = suggested && suggested.toLowerCase().endsWith('.png') ? suggested : `IPPure-${ip}-${Date.now()}.png`;
  const out = path.join(outdir, filename);
  await download.saveAs(out);
  await browser.close();
  console.log(out);
})().catch(err => {
  console.error(err.stack || err.message || String(err));
  process.exit(1);
});
