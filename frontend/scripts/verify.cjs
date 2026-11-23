/**
 * End-to-end verification of the four acceptance criteria against a running app.
 *
 *   npm run build && npm start        # or: npm run dev
 *   npm run verify                    # BASE=http://localhost:3000 by default
 *
 * Uses Playwright's bundled Chromium (`npx playwright install chromium`) or any local
 * Chrome/Chromium via CHROME_PATH=/usr/bin/chromium.
 */
const { chromium } = require('playwright');
const BASE = process.env.BASE || 'http://localhost:3000';
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) process.exitCode = 1; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const browser = await chromium.launch(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {});
  const page = await browser.newPage();
  const errors = []; page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  const reqs = []; page.on('request', r => reqs.push(r.url())); 
  const rsc = () => reqs.filter(u => u.includes('_rsc=')).length;
  const api = () => reqs.filter(u => u.includes('/api/events')).length;
  const counter = async id => Number((await page.textContent(`[data-testid=${id}]`)).match(/\d+$/)[0]);

  // 1. Mount from URL: inputs reflect URL, zero client data fetches
  await page.goto(`${BASE}/?search=billing&severity=critical`); await page.waitForLoadState('networkidle'); await sleep(500);
  ok(await page.inputValue('input[type=search]') === 'billing', 'search input hydrated from URL');
  ok(await page.inputValue('select') === 'critical', 'severity select hydrated from URL');
  const rows1 = await page.locator('[role=option]').count();
  ok(rows1 > 0 && (await page.locator('[role=option] >> text=billing-api').count()) === rows1, `server-filtered list (${rows1} rows)`);
  ok(rsc() === 0 && api() === 0, `no client fetches on mount (rsc=${rsc()}, api=${api()})`);

  // 2. Debounced typing: one navigation for a burst of keystrokes
  await page.goto(BASE); await page.waitForLoadState('networkidle'); await sleep(300);
  reqs.length = 0;
  await page.locator('input[type=search]').pressSequentially('auth-gateway', { delay: 40 });
  ok(rsc() === 0, 'no navigation while typing within debounce window');
  await page.waitForURL(/search=auth-gateway/); await sleep(500);
  ok(rsc() === 1, `exactly one RSC request after debounce (got ${rsc()})`);
  ok(api() === 0, 'no /api/events calls');
  // 3. Severity -> push; Back/Forward
  reqs.length = 0;
  await page.selectOption('select', 'warning'); await page.waitForURL(/severity=warning/); await sleep(400);
  ok(rsc() === 1, `severity change = 1 request (got ${rsc()})`);
  ok(new URL(page.url()).searchParams.get('search') === 'auth-gateway', 'severity change preserves search');
  await page.goBack(); await page.waitForURL(u => !u.toString().includes('severity')); await sleep(400);
  ok(await page.inputValue('select') === 'all' && await page.inputValue('input[type=search]') === 'auth-gateway', 'Back restores previous filters into inputs');
  await page.goBack(); await page.waitForURL(u => !u.toString().includes('search')); await sleep(400);
  ok(await page.inputValue('input[type=search]') === '', 'Back again clears search input');
  await page.goForward(); await page.waitForURL(/search=auth-gateway/); await sleep(400);
  ok(await page.inputValue('input[type=search]') === 'auth-gateway', 'Forward restores search');
  await sleep(800);
  ok(new URL(page.url()).searchParams.get('search') === 'auth-gateway' && !page.url().includes('severity'), 'no stray pushes after history navigation');

  // 4. Stream bleed: A -> B -> A rapidly
  await page.goto(BASE); await page.waitForLoadState('networkidle'); await sleep(300);
  const click = id => page.click(`[data-event-id=${id}]`);
  await click('evt_0001'); await sleep(450);
  await click('evt_0002'); await sleep(350);
  await click('evt_0003'); await sleep(120);
  await click('evt_0001');
  await page.waitForSelector('[data-testid=stream-status][data-status=done]', { timeout: 15000, state: 'attached' });
  const text = await page.textContent('[data-testid=summary-text]');
  ok((text.match(/Incident Analysis for/g) || []).length === 1, 'exactly one report in panel');
  ok(text.includes('evt_0001') && !text.includes('evt_0002') && !text.includes('evt_0003'), 'no chunks from B/C bled into A');
  ok(text.trim().endsWith('cluster.'), 'report complete');

  // 5. Render isolation while streaming
  await click('evt_0010'); await sleep(150);
  const before = { list: await counter('eventlist-renders'), bar: await counter('filterbar-renders'), fmt: await counter('formatter-renders'), sum: await counter('summary-renders') };
  await page.waitForSelector('[data-testid=stream-status][data-status=done]', { timeout: 15000, state: 'attached' });
  const after = { list: await counter('eventlist-renders'), bar: await counter('filterbar-renders'), fmt: await counter('formatter-renders'), sum: await counter('summary-renders') };
  console.log('   counters before', before, 'after', after);
  ok(after.list === before.list && after.bar === before.bar && after.fmt === before.fmt, 'list / filter bar / formatter did not re-render during stream');
  ok(after.sum - before.sum > 20, `summary leaf re-rendered per token (+${after.sum - before.sum})`);

  // 6. Filter change mid-stream aborts and clears
  await click('evt_0020'); await sleep(600);
  await page.selectOption('select', 'critical'); await page.waitForURL(/severity=critical/); await sleep(300);
  ok(await page.locator('[data-testid=summary-text]').count() === 0, 'selection cleared + stream unmounted on filter change');
  await sleep(1500);
  ok(await page.locator('[data-testid=summary-text]').count() === 0, 'no late chunks resurrect the panel');

  ok(errors.length === 0, `no page errors / unhandled AbortError (${JSON.stringify(errors)})`);
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
