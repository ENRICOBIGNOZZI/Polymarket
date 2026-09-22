#!/usr/bin/env python3
from __future__ import annotations
import argparse,re,time
from urllib.parse import urlparse

def visible(loc):
    for i in range(loc.count()):
        x=loc.nth(i)
        try:
            if x.is_visible():
                return x
        except Exception:
            pass
    return None

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--url",required=True)
    p.add_argument("--cdp-url",default="http://127.0.0.1:9222")
    a=p.parse_args()
    if not re.fullmatch(r"https://login\.tailscale\.com/a/[A-Za-z0-9_-]+",a.url):
        p.error("unexpected tailscale device login URL")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser=pw.chromium.connect_over_cdp(a.cdp_url)
        ctx=browser.contexts[0] if browser.contexts else browser.new_context()
        page=ctx.new_page()
        page.goto(a.url,wait_until="domcontentloaded",timeout=60000)
        deadline=time.time()+60
        clicked=False
        while time.time()<deadline:
            body=""
            try:
                body=page.locator("body").inner_text(timeout=3000)
            except Exception:
                pass
            low=body.lower()
            if any(x in low for x in (
                "successfully connected","device is connected","you may close",
                "machine has been added","device authorized","success"
            )):
                print("TAILSCALE_DEVICE_AUTHORIZED=1")
                break
            button=visible(page.get_by_role("button",name=re.compile(
                r"^(Connect|Authorize|Approve|Accept|Log in|Sign in|Add device|Confirm)$",re.I)))
            if button is not None:
                button.click()
                clicked=True
                page.wait_for_timeout(1200)
                continue
            page.wait_for_timeout(750)
        else:
            host=urlparse(page.url).hostname or ""
            path=urlparse(page.url).path
            raise RuntimeError(f"tailscale_device_authorization_timeout host={host} path={path} clicked={clicked}")
        host=urlparse(page.url).hostname or ""
        path=urlparse(page.url).path
        print("FINAL_HOST="+host)
        print("FINAL_PATH="+path)
        browser.close()
    return 0

if __name__=="__main__":
    raise SystemExit(main())
