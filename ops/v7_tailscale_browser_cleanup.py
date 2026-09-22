#!/usr/bin/env python3
# recovery diagnostic trigger
from __future__ import annotations
import argparse, json, re, shutil, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATIC_NAMES = re.compile(r"^(?:github-runnervm[a-z0-9-]+|github-runner[a-z0-9-]+)$", re.I)
RUN_NAMES = re.compile(r"^gh-(?:deploy|health|freeze|inventory)-([0-9]+)-([0-9]+)$", re.I)
EXTRACT = re.compile(r"\b(?:gh-(?:deploy|health|freeze|inventory)-[0-9]+-[0-9]+|github-runnervm[a-z0-9-]+|github-runner[a-z0-9-]+)\b", re.I)
INTERACTIVE_MARKERS = (
    "2-Step Verification", "Verify it's you", "Confirm it's you",
    "Check your phone", "Enter the code", "Security key", "Passkey",
)

def api_json(url: str, token: str) -> dict:
    req=urllib.request.Request(url,headers={
        "Accept":"application/vnd.github+json",
        "Authorization":"Bearer "+token,
        "User-Agent":"polymarket-v7-tailnet-cleanup",
    })
    with urllib.request.urlopen(req,timeout=20) as resp:
        value=json.load(resp)
    if not isinstance(value,dict):
        raise RuntimeError("github_api_invalid")
    return value

def protected_run_ids(repository: str, token: str) -> set[int]:
    value=api_json(f"https://api.github.com/repos/{repository}/actions/runs?per_page=100",token)
    now=datetime.now(timezone.utc)
    protected=set()
    for row in value.get("workflow_runs") or []:
        if not isinstance(row,dict):
            continue
        rid=row.get("id")
        if not isinstance(rid,int):
            continue
        status=str(row.get("status") or "")
        created=row.get("created_at")
        recent=False
        if isinstance(created,str):
            try:
                ts=datetime.fromisoformat(created.replace("Z","+00:00"))
                recent=(now-ts).total_seconds()<1800
            except ValueError:
                pass
        if status!="completed" or recent:
            protected.add(rid)
    return protected

def first_visible(locator):
    for i in range(locator.count()):
        item=locator.nth(i)
        try:
            if item.is_visible():
                return item
        except Exception:
            pass
    return None

def is_google_accounts_url(url: str) -> bool:
    return bool(re.match(r"^https://accounts\.google\.[^/]+(?:/|$)", str(url or ""), re.I))

def resume_tailscale_after_google(page) -> bool:
    """Finish the post-2SV Google SetSID handoff without bypassing authentication."""
    try:
        url=str(page.url or "")
    except Exception:
        url=""
    if not is_google_accounts_url(url):
        return "/admin/machines" in url and "login.tailscale.com" in url
    if "/accounts/SetSID" not in url and "/SetSID" not in url:
        return False

    print("tailnet_auth_google_setsid=true", flush=True)
    page.wait_for_timeout(3000)

    # Some Google locale domains leave the browser on SetSID after the user
    # approved 2SV. Re-entering the original Tailscale URL in the same browser
    # context completes the already-authenticated OAuth handoff.
    page.goto(
        "https://login.tailscale.com/admin/machines",
        wait_until="domcontentloaded",
        timeout=60000,
    )
    page.wait_for_timeout(1200)
    if "/admin/machines" in page.url and "login.tailscale.com" in page.url:
        print("tailnet_auth_setsid_resume_ok=true", flush=True)
        return True

    google=first_visible(page.get_by_text(re.compile(r"Sign in with Google", re.I)))
    if google is not None:
        google.click()
        print("tailnet_auth_setsid_google_reentry=true", flush=True)
        page.wait_for_timeout(1800)
    return "/admin/machines" in page.url and "login.tailscale.com" in page.url

def wait_for_human_2sv(page, timeout_s: int=420) -> bool:
    deadline=time.monotonic()+timeout_s
    prompt_triggered=False
    last_url=""
    while time.monotonic()<deadline:
        try:
            url=page.url
            body=page.locator("body").inner_text(timeout=3000)
        except Exception:
            url,body="",""
        if url != last_url:
            print("tailnet_auth_url_stage="+url.split("?")[0][:160],flush=True)
            last_url=url
        if not is_google_accounts_url(url):
            return True

        if "/accounts/SetSID" in url or "/SetSID" in url:
            if resume_tailscale_after_google(page):
                return True
            page.wait_for_timeout(1000)
            continue
        lower=body.lower()

        # On Google's challenge-selection screen, explicitly choose the normal
        # phone prompt so the user actually receives an approval request.
        if not prompt_triggered and "/challenge/selection" in url:
            chosen=None
            for pattern in (
                r"Google Prompt", r"Tap Yes", r"Check your phone",
                r"Use your phone", r"Get a prompt",
                r"Controlla il telefono", r"Tocca Sì", r"Usa il telefono",
                r"Ricevi un prompt",
            ):
                chosen=first_visible(page.get_by_text(re.compile(pattern,re.I)))
                if chosen is not None:
                    break
            if chosen is not None:
                # Google renders the visible text inside a div while the
                # surrounding <li> owns pointer events. Click the clickable
                # row rather than forcing the inner text node.
                row=chosen.locator("xpath=ancestor::li[1]")
                if row.count() and row.first.is_visible():
                    row.first.click()
                else:
                    chosen.click(force=True)
                prompt_triggered=True
                print("tailnet_auth_google_prompt_triggered=true",flush=True)
                page.wait_for_timeout(1500)
                continue

        if any(marker.lower() in lower for marker in (
            "2-step verification","verify it's you","confirm it's you",
            "check your phone","tap yes","google prompt","passkey",
        )):
            print("tailnet_auth_waiting_for_human_2sv=true",flush=True)
        if any(x in lower for x in (
            "couldn’t sign you in","couldn't sign you in",
            "browser or app may not be secure",
        )):
            raise RuntimeError("google_browser_blocked")
        page.wait_for_timeout(2000)
    return False

def login(page,email,password):
    print("tailnet_cleanup_stage=LOGIN_GOTO", flush=True)
    page.goto("https://login.tailscale.com/admin/machines",wait_until="domcontentloaded",timeout=60000)
    print("tailnet_cleanup_url="+page.url.split("?")[0][:180], flush=True)
    google=first_visible(page.get_by_text(re.compile(r"Sign in with Google",re.I)))
    print("tailnet_cleanup_google_button="+str(google is not None).lower(), flush=True)
    if google is not None:
        google.click()
        print("tailnet_cleanup_stage=GOOGLE_CLICKED", flush=True)
    else:
        email_box=first_visible(page.locator('input[type="email"]'))
        if email_box is not None:
            email_box.fill(email)
            sign=first_visible(page.get_by_role("button",name=re.compile(r"Sign in|Next",re.I)))
            if sign is None:
                raise RuntimeError("tailscale_signin_button_missing")
            sign.click()

    page.wait_for_timeout(1200)
    print("tailnet_cleanup_post_click_url="+page.url.split("?")[0][:180], flush=True)
    if is_google_accounts_url(page.url):
        email_locator=page.locator('input[name="identifier"], input[type="email"]')
        try:
            email_locator.first.wait_for(state="visible",timeout=20000)
        except Exception:
            raise RuntimeError("google_email_field_missing")
        email_box=first_visible(email_locator)
        if email_box is None:
            raise RuntimeError("google_email_field_missing")
        email_box.fill(email)
        if not email_box.input_value():
            raise RuntimeError("google_email_fill_failed")
        print("tailnet_cleanup_google_email_filled=true", flush=True)
        nxt=first_visible(page.locator("#identifierNext")) or first_visible(page.get_by_role("button",name=re.compile(r"Next",re.I)))
        if nxt is None:
            raise RuntimeError("google_email_next_missing")
        nxt.click()
        page.wait_for_timeout(1500)
        if "/signin/identifier" in page.url:
            email_box.press("Enter")
            page.wait_for_timeout(1800)
        print("tailnet_cleanup_post_email_url="+page.url.split("?")[0][:180], flush=True)

        password_box=page.locator('input[name="Passwd"]')
        try:
            password_box.wait_for(state="visible",timeout=20000)
        except Exception:
            body=page.locator("body").inner_text(timeout=5000)
            lower=body.lower()
            safe_url=page.url.split("?")[0][:180]
            safe_flags={
                "identifier": "/signin/identifier" in page.url,
                "challenge": "/challenge/" in page.url,
                "account_chooser": ("choose an account" in lower or "scegli un account" in lower),
                "verify": any(x in lower for x in (
                    "verify it's you","confirm it's you","2-step verification",
                    "check your phone","tap yes","passkey",
                    "verifica che sia tu","conferma la tua identità",
                    "verifica in due passaggi","controlla il telefono",
                    "tocca sì","passkey"
                )),
                "browser_blocked": any(x in lower for x in (
                    "couldn’t sign you in","couldn't sign you in",
                    "browser or app may not be secure",
                    "impossibile accedere","browser o app potrebbero non essere sicuri"
                )),
            }
            print("tailnet_cleanup_google_intermediate="+json.dumps(
                {"url":safe_url,**safe_flags},sort_keys=True
            ),flush=True)
            if safe_flags["browser_blocked"]:
                raise RuntimeError("google_browser_blocked")
            if safe_flags["identifier"]:
                if "couldn’t find your google account" in lower or "couldn't find your google account" in lower:
                    raise RuntimeError("google_account_not_found")
                if "enter a valid email" in lower or "inserisci un indirizzo email valido" in lower:
                    raise RuntimeError("google_identifier_invalid")
                raise RuntimeError("google_identifier_did_not_advance")
            if is_google_accounts_url(page.url):
                # Google may insert challenge/passkey/account-selection screens
                # before or instead of the password screen. Wait through those
                # flows rather than treating them as a missing password field.
                if not wait_for_human_2sv(page,timeout_s=900):
                    raise RuntimeError("interactive_auth_timeout")
                if not is_google_accounts_url(page.url):
                    password_box=None
                else:
                    password_box=page.locator('input[name="Passwd"]')
                    try:
                        password_box.wait_for(state="visible",timeout=10000)
                    except Exception:
                        # If the challenge itself completed authentication,
                        # allow the final Tailscale redirect loop below to decide.
                        password_box=None
            else:
                generic_password=first_visible(page.locator('input[type="password"]'))
                if generic_password is None:
                    password_box=None
                else:
                    generic_password.fill(password)
                    submit=first_visible(page.get_by_role("button",name=re.compile(r"Sign in|Log in|Next|Continue",re.I)))
                    if submit is None:
                        submit=first_visible(page.locator('button[type="submit"], input[type="submit"]'))
                    if submit is None:
                        raise RuntimeError("sso_submit_missing")
                    submit.click()
                    password_box=None
                    page.wait_for_timeout(1500)
        if password_box is not None:
            password_box.fill(password)
            nxt=first_visible(page.locator("#passwordNext")) or first_visible(page.get_by_role("button",name=re.compile(r"Next",re.I)))
            if nxt is None:
                raise RuntimeError("google_password_next_missing")
            nxt.click()
            page.wait_for_timeout(1200)
            try:
                body=page.locator("body").inner_text(timeout=3000)
            except Exception:
                body=""
            if is_google_accounts_url(page.url) and any(marker.lower() in body.lower() for marker in INTERACTIVE_MARKERS):
                if not wait_for_human_2sv(page):
                    raise RuntimeError("interactive_auth_timeout")

    deadline=time.monotonic()+90
    google_reentry_attempted=False
    while time.monotonic()<deadline:
        url=str(page.url or "")
        if "/admin/machines" in url and "login.tailscale.com" in url:
            return

        if is_google_accounts_url(url) and ("/accounts/SetSID" in url or "/SetSID" in url):
            if resume_tailscale_after_google(page):
                return
            page.wait_for_timeout(1000)
            continue

        if "login.tailscale.com" in url and not google_reentry_attempted:
            google=first_visible(page.get_by_text(re.compile(r"Sign in with Google",re.I)))
            if google is not None:
                google.click()
                google_reentry_attempted=True
                print("tailnet_cleanup_google_reentry_clicked=true", flush=True)
                page.wait_for_timeout(1800)
                continue

        page.wait_for_timeout(1000)
    raise RuntimeError("tailscale_admin_login_timeout")

def candidate_names(page):
    search=first_visible(page.locator('input[type="search"]'))
    if search is None:
        search=first_visible(page.get_by_placeholder(re.compile(r"search",re.I)))
    if search is not None:
        search.fill("github")
        page.wait_for_timeout(1200)
    body=page.locator("body").inner_text(timeout=10000)
    names=set(m.group(0) for m in EXTRACT.finditer(body))
    if search is not None:
        search.fill("gh-")
        page.wait_for_timeout(1200)
        body=page.locator("body").inner_text(timeout=10000)
        names.update(m.group(0) for m in EXTRACT.finditer(body))
    return sorted(names)

def row_for_name(page,name):
    label=first_visible(page.get_by_text(name,exact=True))
    if label is None:
        raise RuntimeError("machine_row_missing:"+name)
    for xpath in ("xpath=ancestor::tr[1]","xpath=ancestor::*[@role='row'][1]","xpath=ancestor::div[.//button][1]"):
        row=label.locator(xpath)
        if row.count() and row.first.is_visible():
            return row.first
    raise RuntimeError("machine_row_container_missing:"+name)

def row_is_stale(row):
    text=row.inner_text(timeout=5000).lower()
    # Fail closed: only an explicit offline/last-seen row is deletable.
    return ("offline" in text) or ("last seen" in text)

def remove_one(page,name):
    row=row_for_name(page,name)
    if not row_is_stale(row):
        return False
    buttons=row.locator("button")
    choices=[]
    for i in range(buttons.count()):
        b=buttons.nth(i)
        if not b.is_visible():
            continue
        label=(b.get_attribute("aria-label") or "").lower()
        popup=(b.get_attribute("aria-haspopup") or "").lower()
        title=(b.get_attribute("title") or "").lower()
        if popup=="menu" or any(x in label+" "+title for x in ("more","action","menu","option")):
            choices.append(b)
    if len(choices)==1:
        menu=choices[0]
    elif len(choices)==0 and buttons.count()==1 and buttons.first.is_visible():
        menu=buttons.first
    else:
        raise RuntimeError("ambiguous_machine_menu:"+name)
    menu.click()
    remove=first_visible(page.get_by_role("menuitem",name=re.compile(r"^Remove$",re.I)))
    if remove is None:
        remove=first_visible(page.get_by_text(re.compile(r"^Remove$",re.I),exact=True))
    if remove is None:
        raise RuntimeError("remove_menu_missing:"+name)
    remove.click()
    dialog=first_visible(page.get_by_role("dialog"))
    if dialog is None:
        raise RuntimeError("remove_dialog_missing:"+name)
    confirm=first_visible(dialog.get_by_role("button",name=re.compile(r"^Remove machine$|^Remove$",re.I)))
    if confirm is None:
        raise RuntimeError("remove_confirm_missing:"+name)
    confirm.click()
    page.wait_for_timeout(600)
    return True

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--credentials",type=Path,required=True)
    p.add_argument("--repository",required=True)
    p.add_argument("--github-token",required=True)
    p.add_argument("--chrome",default="google-chrome")
    p.add_argument("--cdp-url",default="")
    a=p.parse_args()
    value=json.loads(a.credentials.read_text(encoding="utf-8"))
    required={"schema","purpose","expected_parent_sha","email","password","maximum_deletions"}
    if set(value)!=required:
        p.error("credential_payload_fields")
    if value["schema"]!="polymarket_v7_tailnet_login_handoff_v2" or value["purpose"]!="TAILNET_CI_QUOTA_RECOVERY":
        p.error("credential_payload_schema")
    maximum=int(value["maximum_deletions"])
    if not 1<=maximum<=120:
        p.error("maximum_deletions")

    print("tailnet_cleanup_stage=ARGS_OK", flush=True)
    protected=protected_run_ids(a.repository,a.github_token)
    print("tailnet_cleanup_stage=PROTECTED_RUNS count="+str(len(protected)), flush=True)
    from playwright.sync_api import sync_playwright
    removed=[]
    skipped_live=[]
    with sync_playwright() as pw:
        print("tailnet_cleanup_stage=PLAYWRIGHT_READY", flush=True)
        chrome=shutil.which(a.chrome) or a.chrome
        if a.cdp_url:
            browser=pw.chromium.connect_over_cdp(a.cdp_url)
            print("tailnet_cleanup_stage=CDP_CONNECTED", flush=True)
            context=browser.contexts[0] if browser.contexts else browser.new_context()
            page=context.pages[0] if context.pages else context.new_page()
        else:
            browser=pw.chromium.launch(
                headless=True,
                executable_path=chrome,
                args=["--disable-dev-shm-usage"],
            )
            print("tailnet_cleanup_stage=BROWSER_LAUNCHED", flush=True)
            context=browser.new_context()
            page=context.new_page()
        print("tailnet_cleanup_stage=LOGIN_BEGIN", flush=True)
        login(page,value["email"],value["password"])
        print("tailnet_cleanup_stage=LOGIN_OK", flush=True)
        names=candidate_names(page)
        print("tailnet_cleanup_stage=CANDIDATES count="+str(len(names)), flush=True)
        for name in names:
            run_match=RUN_NAMES.fullmatch(name)
            if run_match and int(run_match.group(1)) in protected:
                skipped_live.append(name)
                continue
            if not (run_match or STATIC_NAMES.fullmatch(name)):
                continue
            if len(removed)>=maximum:
                raise RuntimeError("deletion_limit_reached")
            if remove_one(page,name):
                print("tailnet_cleanup_removed="+name,flush=True)
                removed.append(name)
            else:
                skipped_live.append(name)
        browser.close()
    print(json.dumps({
        "schema":"polymarket_v7_tailnet_browser_cleanup_v2",
        "removed_count":len(removed),
        "removed_names":removed,
        "skipped_nonstale_or_protected":skipped_live,
    },sort_keys=True))
    if not removed:
        raise SystemExit("no_stale_ci_nodes_removed")
    return 0

if __name__=="__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("tailnet_cleanup_error="+type(exc).__name__+":"+str(exc)[:240], flush=True)
        raise
