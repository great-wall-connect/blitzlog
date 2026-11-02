#!/usr/bin/env node
// Render a docs/architecture/<name>.html file to <name>.png via
// headless Chromium.
//
// Usage:
//   node docs/architecture/render.cjs                       # system-overview (default)
//   node docs/architecture/render.cjs auth-overview          # any other diagram
//
// Issue #164: the script used to hard-code one machine's absolute
// paths (the author's npm prefix and their puppeteer Chromium
// download), so nobody else could re-render a diagram after editing
// the HTML. Both are now resolved at run time:
//
//   * puppeteer is taken from the local node_modules if it is there,
//     otherwise from the globally-installed mermaid-cli (which bundles
//     it);
//   * the Chromium binary is left to puppeteer's own resolution
//     (puppeteer.executablePath()), which finds the browser it
//     downloaded on install.

const path = require('path');
const { execSync } = require('child_process');

function loadPuppeteer() {
  try {
    return require('puppeteer');
  } catch (err) {
    // Not installed locally — fall back to the copy mermaid-cli
    // bundles in its own node_modules.
    const globalRoot = execSync('npm root -g', { encoding: 'utf8' }).trim();
    return require(
      path.join(
        globalRoot,
        '@mermaid-js/mermaid-cli/node_modules/puppeteer'
      )
    );
  }
}

const puppeteer = loadPuppeteer();

const NAME = process.argv[2] || 'system-overview';
const INPUT  = path.join(__dirname, `${NAME}.html`);
const OUTPUT = path.join(__dirname, `${NAME}.png`);

(async () => {
  const browser = await puppeteer.launch({
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1800, height: 1320, deviceScaleFactor: 2 });
  await page.goto('file://' + path.resolve(INPUT), { waitUntil: 'networkidle0' });
  await page.screenshot({ path: OUTPUT, fullPage: true });
  await browser.close();
  console.log(`Rendered ${OUTPUT}`);
})();
