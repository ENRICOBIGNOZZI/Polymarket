#!/usr/bin/env python3
"""Remove only stale GitHub Actions helper nodes from a Tailscale tailnet."""
from __future__ import annotations
import argparse, json, os, re, shutil, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

PREFIXES = ("gh-deploy-", "gh-health-", "gh-freeze-", "gh-inventory-", "gh-mamma-recover-", "github-runnervm")
NAME_RE = re.compile(r"^(gh-(?:deploy|health|freeze|inventory|mamma-recover)-([0-9]+)-([0-9]+))$")
LEGACY_RUNNER_RE = re.compile(r"^github-runnervm[a-z0-9-]+$", re.I)
INTERACTIVE_MARKERS = (
    "2-Step Verification", "Verify it's you", "Confirm it's you",
    "Check your phone", "Enter the code", "Security key", "Passkey",
)

def api_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "polymarket-v7-tailnet-cleanup",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        value = json.load(resp)
    if not isinstance(value, dict):
        raise RuntimeError("github_api_invalid")
    return value

def protected_run_ids(repository: str, token: str) -> set[int]:
    value = api_json(
        f"https://api.github.com/repos/{repository}/actions/runs?per_page=100", token
    )
    now = datetime.now(timezone.utc)
    protected: set[int] = set()
    for row in value.get("workflow_runs") or []:
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        if not isinstance(rid, int):
            continue
        status = str(row.get("status") or "")
        created = row.get("created_at")
        recent = False
        if isinstance(created, str):
            try:
                ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                recent = (now - ts).total_seconds() < 1800
            except ValueError:
                pass
        if status != "completed" or recent:
            protected.add(rid)
    return protected

def first_visible(locator):
    for i in range(locator.count()):
        item = locator.nth(i)
        try:
            if item.is_visible():
                return item
        except Exception:
            pass
    return None

def wait_for_human_2sv(page, timeout_s: int = 420) -> bool:
    """Trigger a normal Google Prompt when offered, then wait for human approval."""
    deadline = time.monotonic() + timeout_s
    last_url = ""
    prompt_triggered = False
    while time.monotonic() < deadline:
        try:
            url = page.url
            body = page.locator("body").inner_text(timeout=3000)
        except Exception:
            url, body = "", ""
        if url != last_url:
            print(f"tailnet_auth_url_stage={url.split('?')[0][:160]}", flush=True)
            last_url = url
        lowered = body.lower()
        if "accounts.google.com" not in url:
            return True

        # Google's challenge/selection page can require choosing the normal
        # phone prompt before any notification is sent. This does not bypass
        # MFA; it only selects the standard user-approval path.
        if not prompt_triggered and "/challenge/selection" in url:
            candidates = [
                r"Google Prompt", r"Tap Yes", r"Check your phone",
                r"Use your phone", r"Get a prompt",
            ]
            chosen = None
            for pattern in candidates:
                chosen = first_visible(page.get_by_text(re.compile(pattern, re.I)))
                if chosen is not None:
                    break
            if chosen is not None:
                clicked = False
                try:
                    target = chosen.locator(
                        "xpath=ancestor::*[@role='link' or @role='button' or @tabindex='0'][1]"
                    )
                    if target.count() and target.first.is_visible():
                        target.first.click(timeout=5000, force=True)
                        clicked = True
                except Exception:
                    clicked = False
                if not clicked:
                    try:
                        chosen.click(timeout=5000, force=True)
                        clicked = True
                    except Exception:
                        clicked = False
                if not clicked:
                    print("tailnet_auth_google_prompt_click_deferred=true", flush=True)
                    page.wait_for_timeout(1200)
                    continue
                prompt_triggered = True
                print("tailnet_auth_google_prompt_triggered=true", flush=True)
                page.wait_for_timeout(1500)
                continue

        if any(marker.lower() in lowered for marker in (
            "2-step verification", "verify it's you", "confirm it's you",
            "check your phone", "tap yes", "google prompt", "passkey",
        )):
            print("tailnet_auth_waiting_for_human_2sv=true", flush=True)
        if any(x in lowered for x in (
            "couldn’t sign you in", "couldn't sign you in",
            "browser or app may not be secure",
        )):
            raise RuntimeError("google_browser_blocked")
        page.wait_for_timeout(2000)
    return False

def login(page, email: str, password: str) -> None:
    page.goto("https://login.tailscale.com/admin/machines",
              wait_until="domcontentloaded", timeout=60000)
    google = first_visible(page.get_by_text(re.compile(r"Sign in with Google", re.I)))
    if google is not None:
        google.click()
    else:
        email_box = first_visible(page.locator('input[type="email"]'))
        if email_box is not None:
            email_box.fill(email)
            sign = first_visible(page.get_by_role("button", name=re.compile(r"Sign in|Next", re.I)))
            if sign is None:
                raise RuntimeError("tailscale_signin_button_missing")
            sign.click()

    page.wait_for_timeout(1200)
    if "accounts.google.com" in page.url:
        email_box = first_visible(page.locator('input[name="identifier"], input[type="email"]'))
        if email_box is not None:
            email_box.fill(email)
            nxt = first_visible(page.locator("#identifierNext"))
            if nxt is None:
                nxt = first_visible(page.get_by_role("button", name=re.compile(r"Next", re.I)))
            if nxt is None:
                raise RuntimeError("google_email_next_missing")
            nxt.click()
        password_box = page.locator('input[name="Passwd"]')
        try:
            password_box.wait_for(state="visible", timeout=20000)
        except Exception:
            body = page.locator("body").inner_text(timeout=5000)
            lowered = body.lower()
            if any(marker.lower() in lowered for marker in INTERACTIVE_MARKERS):
                if not wait_for_human_2sv(page):
                    raise RuntimeError("interactive_auth_timeout")
                password_box = page.locator('input[name="Passwd"]')
                try:
                    password_box.wait_for(state="visible", timeout=5000)
                except Exception:
                    # Approval may have completed the whole Google flow.
                    if "accounts.google.com" not in page.url:
                        password_box = None
                    else:
                        raise RuntimeError("interactive_auth_incomplete")
                if password_box is None:
                    pass
                else:
                    password_box.fill(password)
                    nxt = first_visible(page.locator("#passwordNext"))
                    if nxt is None:
                        nxt = first_visible(page.get_by_role("button", name=re.compile(r"Next", re.I)))
                    if nxt is None:
                        raise RuntimeError("google_password_next_missing")
                    nxt.click()
            safe_markers = (
                ("google_browser_blocked", ("couldn’t sign you in", "couldn't sign you in", "browser or app may not be secure")),
                ("google_account_not_found", ("couldn’t find your google account", "couldn't find your google account")),
                ("google_identifier_invalid", ("enter a valid email", "enter a valid email or phone number")),
                ("google_account_chooser", ("choose an account",)),
            )
            for code, markers in safe_markers:
                if any(marker in lowered for marker in markers):
                    raise RuntimeError(code)
            raise RuntimeError("google_password_field_missing")
        if password_box is not None:
            password_box.fill(password)
            nxt = first_visible(page.locator("#passwordNext"))
            if nxt is None:
                nxt = first_visible(page.get_by_role("button", name=re.compile(r"Next", re.I)))
            if nxt is None:
                raise RuntimeError("google_password_next_missing")
            nxt.click()
            page.wait_for_timeout(1200)
            try:
                body = page.locator("body").inner_text(timeout=3000)
            except Exception:
                body = ""
            if "accounts.google.com" in page.url and any(
                marker.lower() in body.lower() for marker in INTERACTIVE_MARKERS
            ):
                if not wait_for_human_2sv(page):
                    raise RuntimeError("interactive_auth_timeout")

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if "/admin/machines" in page.url and "login.tailscale.com" in page.url:
            return
        try:
            body = page.locator("body").inner_text(timeout=3000)
        except Exception:
            body = ""
        if any(marker.lower() in body.lower() for marker in INTERACTIVE_MARKERS):
            if not wait_for_human_2sv(page, timeout_s=900):
                raise RuntimeError("interactive_auth_timeout")
            if "/admin/machines" in page.url and "login.tailscale.com" in page.url:
                return
        page.wait_for_timeout(1000)
    raise RuntimeError("tailscale_admin_login_timeout")

def candidate_names(page) -> list[str]:
    search = first_visible(page.locator('input[type="search"]'))
    if search is None:
        search = first_visible(page.get_by_placeholder(re.compile(r"search", re.I)))
    if search is not None:
        search.fill("gh-")
        page.wait_for_timeout(1000)
    body = page.locator("body").inner_text(timeout=10000)
    names=set(m.group(1) for m in re.finditer(
        r"\b(gh-(?:deploy|health|freeze|inventory|mamma-recover)-[0-9]+-[0-9]+)\b", body
    ))
    names.update(m.group(1) for m in re.finditer(
        r"\b(github-runnervm[a-z0-9-]+)\b", body, re.I
    ))
    return sorted(names)

def row_for_name(page, name: str):
    label = first_visible(page.get_by_text(name, exact=True))
    if label is None:
        raise RuntimeError(f"machine_row_missing:{name}")
    for xpath in (
        "xpath=ancestor::tr[1]",
        "xpath=ancestor::*[@role='row'][1]",
        "xpath=ancestor::div[.//button][1]",
    ):
        row = label.locator(xpath)
        if row.count() and row.first.is_visible():
            return row.first
    raise RuntimeError(f"machine_row_container_missing:{name}")

def remove_one(page, name: str) -> None:
    row = row_for_name(page, name)
    buttons = row.locator("button")
    choices = []
    for i in range(buttons.count()):
        b = buttons.nth(i)
        if not b.is_visible():
            continue
        label = (b.get_attribute("aria-label") or "").lower()
        popup = (b.get_attribute("aria-haspopup") or "").lower()
        title = (b.get_attribute("title") or "").lower()
        if popup == "menu" or any(x in label + " " + title for x in ("more", "action", "menu", "option")):
            choices.append(b)
    if len(choices) == 1:
        menu = choices[0]
    elif len(choices) == 0 and buttons.count() == 1 and buttons.first.is_visible():
        menu = buttons.first
    else:
        raise RuntimeError(f"ambiguous_machine_menu:{name}")
    menu.click()
    remove = first_visible(page.get_by_role("menuitem", name=re.compile(r"^Remove$", re.I)))
    if remove is None:
        remove = first_visible(page.get_by_text(re.compile(r"^Remove$", re.I), exact=True))
    if remove is None:
        raise RuntimeError(f"remove_menu_missing:{name}")
    remove.click()
    dialog = first_visible(page.get_by_role("dialog"))
    if dialog is None:
        raise RuntimeError(f"remove_dialog_missing:{name}")
    text = dialog.inner_text(timeout=5000)
    if "remove" not in text.lower():
        raise RuntimeError(f"remove_dialog_invalid:{name}")
    confirm = first_visible(dialog.get_by_role(
        "button", name=re.compile(r"^Remove machine$|^Remove$", re.I)
    ))
    if confirm is None:
        raise RuntimeError(f"remove_confirm_missing:{name}")
    confirm.click()
    page.wait_for_timeout(600)

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--credentials", type=Path, required=True)
    p.add_argument("--repository", required=True)
    p.add_argument("--github-token", required=True)
    p.add_argument("--chrome", default="")
    p.add_argument("--cdp-url", default="")
    a = p.parse_args()
    value = json.loads(a.credentials.read_text(encoding="utf-8"))
    required = {
        "schema","purpose","expected_main_sha","email","password",
        "allowed_prefixes","maximum_deletions",
    }
    if set(value) != required:
        p.error("credential_payload_fields")
    if value["schema"] != "polymarket_v7_tailnet_login_handoff_v1" or value["purpose"] != "TAILNET_CI_QUOTA_RECOVERY":
        p.error("credential_payload_schema")
    allowed_prefixes=tuple(str(x) for x in value["allowed_prefixes"])
    safe_prefixes=set(PREFIXES)
    if not allowed_prefixes or any(x not in safe_prefixes for x in allowed_prefixes):
        p.error("prefix_contract")
    maximum = int(value["maximum_deletions"])
    if not 1 <= maximum <= 24:
        p.error("maximum_deletions")
    protected = protected_run_ids(a.repository, a.github_token)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise SystemExit(f"playwright_import_failed:{type(exc).__name__}")
    chrome = a.chrome or shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        raise SystemExit("chrome_not_found")

    removed: list[str] = []
    candidates: list[str] = []
    with sync_playwright() as pw:
        if a.cdp_url:
            browser = pw.chromium.connect_over_cdp(a.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            pages = context.pages
            page = pages[0] if pages else context.new_page()
        else:
            browser = pw.chromium.launch(
                headless=True, executable_path=chrome,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            context = browser.new_context()
            page = context.new_page()
        login(page, value["email"], value["password"])
        candidates = candidate_names(page)
        for name in candidates:
            if not any(name.startswith(prefix) for prefix in allowed_prefixes):
                continue
            match = NAME_RE.fullmatch(name)
            legacy = bool(LEGACY_RUNNER_RE.fullmatch(name))
            if not match and not legacy:
                continue
            row = row_for_name(page, name)
            row_text = row.inner_text(timeout=5000).lower()
            if "offline" not in row_text and "last seen" not in row_text:
                continue
            if match:
                run_id = int(match.group(2))
                if run_id in protected:
                    continue
            if len(removed) >= maximum:
                raise RuntimeError("deletion_limit_reached")
            remove_one(page, name)
            removed.append(name)
        if not a.cdp_url:
            context.close()
        browser.close()

    if not candidates:
        raise SystemExit("no_ci_nodes_visible")
    if not removed:
        raise SystemExit("no_stale_ci_nodes_removed")
    print(json.dumps({
        "schema":"polymarket_v7_tailnet_ci_cleanup_v1",
        "candidate_count":len(candidates),
        "protected_count":sum(1 for n in candidates if NAME_RE.fullmatch(n) and int(NAME_RE.fullmatch(n).group(2)) in protected),
        "removed_count":len(removed),
        "removed_names":removed,
        "allowed_prefixes":list(allowed_prefixes),
    }, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
