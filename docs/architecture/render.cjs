#!/usr/bin/env node
// Render docs/architecture/system-overview.html → system-overview.png via
// headless Chromium. The bundled puppeteer module ships with mermaid-cli.
//
// Usage: node docs/architecture/render.cjs

const path = require('path');
const puppeteer = require('/home/dsarosi/.npm-global/lib/node_modules/@mermaid-js/mermaid-cli/node_modules/puppeteer');

const INPUT  = path.join(__dirname, 'system-overview.html');
const OUTPUT = path.join(__dirname, 'system-overview.png');

(async () => {
  const browser = await puppeteer.launch({
    executablePath: '/home/dsarosi/.cache/puppeteer/chrome-headless-shell/linux-131.0.6778.204/chrome-headless-shell-linux64/chrome-headless-shell',
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1800, height: 1320, deviceScaleFactor: 2 });
  await page.goto('file://' + path.resolve(INPUT), { waitUntil: 'networkidle0' });
  await page.screenshot({ path: OUTPUT, fullPage: true });
  await browser.close();
  console.log(`Rendered ${OUTPUT}`);
})();