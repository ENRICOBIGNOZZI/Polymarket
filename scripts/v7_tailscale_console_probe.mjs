import { chromium } from 'playwright-core';

function marker(name, value = 'true') {
  process.stdout.write(`${name}=${value}\n`);
}

async function firstVisible(page, selectors) {
  for (const selector of selectors) {
    const loc = page.locator(selector).first();
    try {
      if (await loc.isVisible({ timeout: 1200 })) return loc;
    } catch {}
  }
  return null;
}

const email = process.env.V7_TS_ADMIN_EMAIL || '';
const password = process.env.V7_TS_ADMIN_PASSWORD || '';
if (!email || !password) throw new Error('admin credentials missing');

const executablePath = process.env.CHROME_BIN || '/usr/bin/google-chrome';
const browser = await chromium.launch({
  headless: process.env.V7_TS_HEADED !== 'true',
  executablePath,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  locale: 'en-US',
});
const page = await context.newPage();
page.setDefaultTimeout(10000);

try {
  await page.goto('https://login.tailscale.com/login', { waitUntil: 'domcontentloaded', timeout: 30000 });
  marker('V7_TS_STAGE', 'TAILSCALE_LOGIN');

  let input = await firstVisible(page, [
    'input[type="email"]',
    'input[name="email"]',
    'input[placeholder*="email" i]',
  ]);
  if (input) {
    await input.fill(email);
    const signIn = await firstVisible(page, [
      'button:has-text("Sign in")',
      'input[type="submit"]',
    ]);
    if (!signIn) throw new Error('tailscale sign-in control missing');
    await Promise.all([
      page.waitForLoadState('domcontentloaded').catch(() => {}),
      signIn.click(),
    ]);
  }

  await page.waitForTimeout(1500);
  if (page.url().includes('accounts.google.com')) {
    marker('V7_TS_STAGE', 'GOOGLE_IDP');
    const googleEmail = await firstVisible(page, ['#identifierId', 'input[type="email"]']);
    if (googleEmail) {
      await googleEmail.fill(email);
      const next = await firstVisible(page, ['#identifierNext button', '#identifierNext']);
      if (!next) throw new Error('google identifier next missing');
      await next.click();
      await page.waitForTimeout(1800);
    }

    const passwordInput = await firstVisible(page, ['input[type="password"]']);
    if (!passwordInput) {
      const prePasswordBody = (await page.locator('body').innerText().catch(() => '')).toLowerCase();
      marker('V7_TS_LOGIN_CHALLENGE', 'true');
      if (/couldn.?t sign you in|browser or app may not be secure|this browser|not secure/.test(prePasswordBody)) {
        marker('V7_TS_CHALLENGE_KIND', 'BROWSER_POLICY');
      } else if (/2-step|two-step|google prompt|tap yes|phone|device|passkey/.test(prePasswordBody)) {
        marker('V7_TS_CHALLENGE_KIND', 'DEVICE_OR_2SV');
      } else if (/captcha/.test(prePasswordBody)) {
        marker('V7_TS_CHALLENGE_KIND', 'CAPTCHA');
      } else if (/verify|confirm|challenge|recovery/.test(prePasswordBody)) {
        marker('V7_TS_CHALLENGE_KIND', 'GENERIC_VERIFICATION');
      } else if (/choose an account|use another account|account/.test(prePasswordBody)) {
        marker('V7_TS_CHALLENGE_KIND', 'ACCOUNT_SELECTION');
      } else {
        marker('V7_TS_CHALLENGE_KIND', 'UNKNOWN_PREPASSWORD');
      }
      marker('V7_TS_AUTHENTICATED', 'false');
      process.exitCode = 3;
    } else {
      await passwordInput.fill(password);
      const passwordNext = await firstVisible(page, ['#passwordNext button', '#passwordNext']);
      if (!passwordNext) throw new Error('google password next missing');
      await passwordNext.click();
      await page.waitForTimeout(3500);

      if (page.url().includes('accounts.google.com')) {
        const body = (await page.locator('body').innerText().catch(() => '')).toLowerCase();
        if (/verify|challenge|2-step|two-step|try another way|confirm|captcha|couldn.?t sign you in/.test(body)) {
          marker('V7_TS_LOGIN_CHALLENGE', 'true');
          if (/couldn.?t sign you in|browser or app may not be secure|this browser|not secure/.test(body)) {
            marker('V7_TS_CHALLENGE_KIND', 'BROWSER_POLICY');
          } else if (/2-step|two-step|google prompt|tap yes|phone|device/.test(body)) {
            marker('V7_TS_CHALLENGE_KIND', 'DEVICE_OR_2SV');
          } else if (/captcha/.test(body)) {
            marker('V7_TS_CHALLENGE_KIND', 'CAPTCHA');
          } else if (/verify|confirm|challenge/.test(body)) {
            marker('V7_TS_CHALLENGE_KIND', 'GENERIC_VERIFICATION');
          } else {
            marker('V7_TS_CHALLENGE_KIND', 'UNKNOWN');
          }
          marker('V7_TS_AUTHENTICATED', 'false');
          process.exitCode = 3;
        }
      }
    }
  }

  if (!process.exitCode) {
    await page.goto('https://login.tailscale.com/admin/machines', {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    await page.waitForTimeout(2000);
    const url = page.url();
    const body = await page.locator('body').innerText().catch(() => '');
    const authenticated = /console\.tailscale\.com|\/admin\/machines/.test(url)
      && !/sign in with google|enter your email/i.test(body);
    marker('V7_TS_AUTHENTICATED', authenticated ? 'true' : 'false');
    if (!authenticated) {
      marker('V7_TS_LOGIN_CHALLENGE', 'true');
      process.exitCode = 3;
    } else {
      const matches = body.match(/gh-deploy-[A-Za-z0-9.-]+/g) || [];
      marker('V7_TS_GH_DEPLOY_VISIBLE_COUNT', String(new Set(matches).size));
      marker('V7_TS_QUOTA_TEXT_VISIBLE', /quota|device limit|node limit|machine limit/i.test(body) ? 'true' : 'false');
      marker('V7_TS_READ_ONLY_PROBE', 'success');
    }
  }
} finally {
  await context.close();
  await browser.close();
}
