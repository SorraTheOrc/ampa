/**
 * Browser smoke test — proves whether the current environment has the
 * system/runtime dependencies required to launch Playwright's Chromium.
 *
 * Pass:  host with Playwright and Chromium installed.
 * Fail:  ampa-dev:latest container (missing system deps), demonstrating
 *        the gap that SA-0MN1M2L330YNA1FU will address.
 *
 * Run:
 *   node --test tests/node/test-browser-smoke.mjs
 *   npm run test:smoke:node
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

test('Chromium launches headlessly and can navigate to about:blank', async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.goto('about:blank');
    const title = await page.title();
    // about:blank has an empty string title — assert it is defined (not null/undefined)
    assert.notEqual(title, null, 'page.title() should not be null');
    assert.notEqual(title, undefined, 'page.title() should not be undefined');
  } finally {
    await browser.close();
  }
test('Chromium launches headlessly and can navigate to about:blank', async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.goto('about:blank');
    const title = await page.title();
    // about:blank has an empty string title — assert it is defined (not null/undefined)
    assert.notEqual(title, null, 'page.title() should not be null');
    assert.notEqual(title, undefined, 'page.title() should not be undefined');
  } finally {
    await browser.close();
  }
>>>>>>> main
});
