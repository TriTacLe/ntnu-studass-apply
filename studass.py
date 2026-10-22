#!/usr/bin/env python3
import argparse
import json
import os
import re
import socket
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

BASE = "https://ie.ntnu.no/studass/"
SSO = "https://innsida.ntnu.no/sso/?target=ie-studass"
APPLY_PAGE = BASE + "?page=soknad_opprett"
PROFILE_PAGE = BASE + "?page=student_endre_data"

REPO = Path(__file__).resolve().parent
OUT = REPO / "out"
STATE_DIR = Path.home() / ".local/share/studass"
PROFILE_DIR = STATE_DIR / "profile"
STATE_FILE = STATE_DIR / "storage_state.json"

EXIT_OK = 0
EXIT_NO_SESSION = 2
EXIT_ROUND_CLOSED = 3
EXIT_UNVERIFIED = 4
EXIT_UNREACHABLE = 5

NTNU_USER = os.environ.get("NTNU_USER", "trile@stud.ntnu.no")
LOGIN_TIMEOUT_MS = 600_000
IDP_HOSTS = ("idp.feide.no", "auth.dataporten.no", "login.microsoftonline.com", "login.microsoft.com")

COURSE_CODE = re.compile(r"\b([A-ZÆØÅ]{2,6}\d{4})\b")
ELEVEN_DIGITS = re.compile(r"\b\d{11}\b")
DEADLINE = re.compile(r"frist[^0-9]{0,40}(\d{1,2}[.\-/]\s?\d{1,2}[.\-/]\s?\d{2,4})", re.I)
SUBMIT_TEXT = re.compile(r"legg inn|søk|send|lagre", re.I)
JUNK_LABEL = re.compile(r"^\s*test", re.I)
GJOVIK = re.compile(r"gj.vik", re.I)
GJOVIK_CODE = re.compile(r"^(IDATG|IELEG|ELEG|IDG|PROG|G1001)", re.I)
TIERS = ("TDT IT DCST DIFT ITO IDI IDATT", "TTM IIK IIKT", "TTK", "MA TMA IMAT IMAG TFOR")


def die(code, message):
    print(message, file=sys.stderr)
    sys.exit(code)


def reachable(host="ie.ntnu.no", port=443, timeout=10):
    try:
        ip = socket.gethostbyname(host)
    except OSError as exc:
        return False, f"DNS lookup for {host} failed: {exc}"
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True, f"{host} ({ip}) accepts TCP on {port}"
    except OSError as exc:
        return False, (
            f"no TCP handshake with {host} ({ip}) on {port}: {exc}\n"
            "The portal only answers from inside the NTNU network. Bring up the VPN first:\n"
            "  openconnect --protocol=anyconnect vpn.ntnu.no\n"
            "or use the Cisco Secure Client, then run preflight again."
        )


@contextmanager
def portal(headless=True):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1400, "height": 1000},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'credentials', {get: () => ({"
            "get: () => Promise.reject(new DOMException('NotAllowedError')),"
            "create: () => Promise.reject(new DOMException('NotAllowedError'))}), configurable: true});"
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(30000)
        try:
            yield ctx, page
        finally:
            ctx.close()


def goto(page, url, attempts=3):
    last = None
    for n in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return
        except PlaywrightTimeout as exc:
            last = exc
            page.wait_for_timeout(2000 * (n + 1))
    die(EXIT_UNREACHABLE, f"could not load {url}: {last}")


def logged_in(page):
    body = page.inner_text("body")
    return bool(re.search(r"Logget inn som|Logg ut", body))


def require_session(page, url=APPLY_PAGE):
    goto(page, url)
    if not logged_in(page):
        die(EXIT_NO_SESSION, "session expired, run: studass.py login")


def username(page):
    goto(page, PROFILE_PAGE)
    field = page.query_selector("input[name*='bruker' i], input[id*='bruker' i]")
    if field:
        value = (field.get_attribute("value") or "").strip()
        if value:
            return value
    match = re.search(r"Brukernavn[^A-Za-z0-9]{0,20}([a-z0-9]{3,12})", page.inner_text("body"))
    return match.group(1) if match else None


def round_title(page):
    for selector in ("h1", "h2", "title"):
        node = page.query_selector(selector)
        if node:
            text = (node.inner_text() if selector != "title" else node.inner_text()).strip()
            if text:
                return text
    return ""


def deadline(page):
    match = DEADLINE.search(page.inner_text("body"))
    return match.group(1).replace(" ", "") if match else None


def discover_form(page):
    """Find the course select, the comment field and the submit control."""
    best = None
    for handle in page.query_selector_all("select"):
        options = []
        for option in handle.query_selector_all("option"):
            value = option.get_attribute("value") or ""
            label = (option.inner_text() or "").strip()
            if not value or value == "0":
                continue
            code = COURSE_CODE.search(label)
            options.append({"value": value, "label": label, "code": code.group(1) if code else ""})
        if options and (best is None or len(options) > len(best["options"])):
            name = handle.get_attribute("name") or handle.get_attribute("id") or ""
            best = {"handle": handle, "name": name, "options": options}
    comment = page.query_selector("textarea")
    submit = None
    for handle in page.query_selector_all("input[type=submit], button, input[type=button]"):
        label = (handle.get_attribute("value") or handle.inner_text() or "").strip()
        if SUBMIT_TEXT.search(label):
            submit = handle
            break
    return best, comment, submit


def active_applications(page):
    """Rows of Mine søknader. Every row carries withdraw and reorder links, codeless
    listings like TFE4120lab included, so counting them is safe to compare."""
    rows = []
    for row in page.query_selector_all("table tr"):
        if row.query_selector("a[href*='soknad=']"):
            rows.append(" ".join((row.inner_text() or "").split()))
    return rows


def applied(rows, code):
    return bool(code) and any(code in row for row in rows)


def offered(page):
    select, _, _ = discover_form(page)
    return {o["value"] for o in select["options"]} if select else set()


def tier(code):
    for n, group in enumerate(TIERS):
        if any(code.startswith(prefix) for prefix in group.split()):
            return n
    return len(TIERS)


def priority_order():
    path = REPO / "priority.txt"
    if not path.exists():
        return []
    codes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if line:
            codes.append(line)
    return codes


def comment_text():
    path = REPO / "comment.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def keep(option, only, exclude, campus):
    if JUNK_LABEL.match(option["label"]):
        return False
    if only and option["code"] not in only:
        return False
    if option["code"] and option["code"] in (exclude or []):
        return False
    if campus == "trondheim":
        if GJOVIK.search(option["label"]) or GJOVIK_CODE.match(option["label"]):
            return False
    return True


def ordered(options, only=None, exclude=None, campus=None):
    wanted = [o for o in options if keep(o, only, exclude, campus)]
    ranking = priority_order()
    def key(option):
        try:
            return (0, ranking.index(option["code"]), "")
        except ValueError:
            return (1, tier(option["code"]), option["label"])
    return sorted(wanted, key=key)


def scrub(html):
    html = ELEVEN_DIGITS.sub("REDACTED", html)
    return re.sub(
        r"(<input[^>]*(?:fodsel|f%C3%B8dsel|person)[^>]*value=\")[^\"]*(\")",
        r"\1REDACTED\2",
        html,
        flags=re.I,
    )


def codes_arg(value):
    return [c.strip().upper() for c in value.split(",") if c.strip()] if value else []


def host(url):
    return urlparse(url).hostname or ""


def password(user):
    """Password from a file, the keyring, or the environment. Empty means type it yourself."""
    path = os.environ.get("NTNU_PASSWORD_FILE", "")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    try:
        found = subprocess.run(
            ["secret-tool", "lookup", "service", "studass", "username", user],
            capture_output=True, text=True, timeout=5,
        )
        if found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.environ.get("NTNU_PASSWORD", "")


def pick_ntnu(page):
    """Feide org selector: choose NTNU and submit."""
    page.evaluate("""() => {
        const field = document.getElementById('org_selector');
        if (field) field.value = 'feide|realm|ntnu.no';
        const form = document.getElementById('discoform_feide');
        if (form) form.submit();
    }""")


def pick_microsoft(page):
    for selector in ("#microsoft-signin-button", "form[action*='login-azuread'] input[type=submit]"):
        box = page.locator(selector)
        if box.count():
            box.first.click(force=True, timeout=2000)
            return True
    for label in ("Bruk arbeids- eller skolekonto", "Logg inn med Microsoft", "Use work or school account"):
        box = page.locator(f"button:has-text('{label}'), a:has-text('{label}')")
        if box.count():
            box.first.click(force=True, timeout=2000)
            return True
    return False


def fill_credentials(page, user, secret):
    for selector in ("input[type=email]", "input[name=loginfmt]", "input[name=username]"):
        field = page.locator(selector)
        if field.count() and field.first.is_visible() and not field.first.input_value():
            field.first.fill(user)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
            return
    if secret:
        field = page.locator("input[type=password]")
        if field.count() and field.first.is_visible() and not field.first.input_value():
            field.first.fill(secret)
            page.keyboard.press("Enter")


def click_text(page, *labels):
    for label in labels:
        box = page.locator(f"button:has-text('{label}'), a:has-text('{label}'), input[value='{label}']")
        if box.count():
            try:
                box.first.click(force=True, timeout=2000)
                return True
            except PlaywrightTimeout:
                pass
    return False


def mfa_number(page):
    box = page.locator("#idRichContext_DisplaySign")
    if box.count():
        text = (box.first.text_content(timeout=1000) or "").strip()
        if text.isdigit():
            return text
    return ""


def drive_login(page, user, timeout_ms=LOGIN_TIMEOUT_MS):
    """Walk Feide and Entra as far as it goes. Returns once the portal is reachable."""
    secret = password(user)
    if not secret:
        print("No stored password, type it in the window when Microsoft asks.")
    shown = ""
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(2000)
        waited += 2000
        where = host(page.url)
        if where == "ie.ntnu.no":
            if logged_in(page):
                return True
            goto(page, APPLY_PAGE)
            continue
        if where not in IDP_HOSTS:
            continue
        if where == "auth.dataporten.no":
            tile = page.locator("#accounts a.section-list")
            if tile.count():
                tile.first.click(timeout=2000)
                continue
            pick_ntnu(page)
            pick_microsoft(page)
            continue
        if where == "idp.feide.no":
            if not pick_microsoft(page):
                fill_credentials(page, user, secret)
            continue
        if "fido" in page.url:
            click_text(page, "Sign in another way", "Logg på på en annen måte")
            page.keyboard.press("Escape")
            continue
        number = mfa_number(page)
        if number:
            if number != shown:
                print(f"MFA number: {number}  (type it in Authenticator)")
                shown = number
            continue
        if click_text(page, "Ja", "Yes"):          # stay signed in, keeps the session alive
            continue
        if click_text(page, "Approve a request on my Microsoft Authenticator app", "Approve a request"):
            continue
        fill_credentials(page, user, secret)
    return False


def cmd_preflight(args):
    ok, message = reachable()
    print(message)
    sys.exit(EXIT_OK if ok else EXIT_UNREACHABLE)


def cmd_login(args):
    ok, message = reachable()
    if not ok:
        die(EXIT_UNREACHABLE, message)
    with portal(headless=False) as (ctx, page):
        goto(page, SSO)
        print(f"Logging in as {args.user}. NTNU is picked for you, MFA is yours.")
        if not drive_login(page, args.user):
            die(EXIT_NO_SESSION, "login did not finish in time, run it again")
        who = username(page)
        print("Brukernavn:", who or "could not read it from the profile page")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(STATE_FILE))
        print("Session saved to", PROFILE_DIR)


def cmd_dump(args):
    raw = OUT / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    with portal(headless=not args.headed) as (ctx, page):
        require_session(page)
        for name, url in (("front", BASE), ("apply", APPLY_PAGE), ("profile", PROFILE_PAGE)):
            goto(page, url)
            target = raw / f"{name}-{stamp}.html"
            target.write_text(scrub(page.content()), encoding="utf-8")
            print("wrote", target)


def cmd_list(args):
    with portal(headless=not args.headed) as (ctx, page):
        require_session(page)
        title = round_title(page)
        print("Round:", title or "unknown")
        if re.search(r"Høst", title, re.I) and not re.search(r"2027", title):
            die(EXIT_ROUND_CLOSED, "the portal still shows an autumn round, the spring round is not open")
        print("Deadline:", deadline(page) or "not stated on this page")
        select, _, _ = discover_form(page)
        if not select:
            die(EXIT_ROUND_CLOSED, "no course select on the application page, so nothing is on offer")
        active = active_applications(page)
        for option in ordered(select["options"], campus=args.campus):
            mark = "already applied" if applied(active, option["code"]) else ""
            print(f"  {option['code']:<10} {option['label'][:70]:<70} {mark}")
        OUT.mkdir(parents=True, exist_ok=True)
        payload = {
            "round": title,
            "deadline": deadline(page),
            "courses": [{k: o[k] for k in ("code", "label", "value")} for o in select["options"]],
            "active": active,
        }
        path = OUT / f"courses-{date.today().isoformat()}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print("wrote", path)


def cmd_status(args):
    with portal(headless=not args.headed) as (ctx, page):
        require_session(page)
        active = active_applications(page)
        if not active:
            print("Aktive søknader: none registered")
            return
        print(f"Aktive søknader: {len(active)}")
        for row in active:
            print(" ", row)


def cmd_apply(args):
    only = codes_arg(args.only)
    exclude = codes_arg(args.exclude)
    if not only and not args.all:
        die(EXIT_UNVERIFIED, "pass --all or --only CODE,CODE")
    comment = comment_text()
    shots = OUT / "screenshots"
    with portal(headless=not args.headed) as (ctx, page):
        require_session(page)
        title = round_title(page)
        select, comment_field, submit = discover_form(page)
        if not select:
            die(EXIT_ROUND_CLOSED, "no course select on the application page")
        plan = ordered(select["options"], only, exclude, args.campus)
        active = active_applications(page)
        print(f"Round: {title}")
        print(f"{len(plan)} listing(s) in priority order, {len(active)} already registered")
        if not args.submit:
            for n, option in enumerate(plan, 1):
                print(f"  {n:>3}. {option['label'][:72]:<72} would submit")
            print("\nComment that would be sent:\n" + (comment or "(comment.txt is empty)"))
            print("\nDry run. Add --submit to register these.")
            return
        shots.mkdir(parents=True, exist_ok=True)
        failed = []
        for n, option in enumerate(plan, 1):
            name = option["label"][:50]
            goto(page, APPLY_PAGE)
            select, comment_field, submit = discover_form(page)
            if not (select and submit):
                failed.append((name, "form controls not found"))
                continue
            if option["value"] not in {o["value"] for o in select["options"]}:
                print(f"{n:>3}. {name} no longer on offer, skipping")
                continue
            before = len(active_applications(page))
            field = select["name"] if re.fullmatch(r"[A-Za-z_][\w-]*", select["name"] or "") else ""
            page.select_option(f"select[name='{field}']" if field else "select", option["value"])
            if comment_field and comment:
                comment_field.fill(comment)
            for box in page.query_selector_all("form input[type=checkbox][name*=eula i]"):
                if not box.is_checked():
                    box.check()
            submit.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1200)
            gone = option["value"] not in offered(page)
            grew = len(active_applications(page)) > before
            if gone and grew:
                print(f"{n:>3}. {name} registered")
            else:
                failed.append((name, f"gone={gone} grew={grew}"))
                print(f"{n:>3}. {name} submit not confirmed")
                page.screenshot(
                    path=str(shots / f"failed-{option['value']}-{date.today().isoformat()}.png"),
                    full_page=True,
                )
        cmd_status_inline(page)
        if failed:
            for name, why in failed:
                print(f"unconfirmed: {name}: {why}", file=sys.stderr)
            sys.exit(EXIT_UNVERIFIED)


def cmd_status_inline(page):
    goto(page, APPLY_PAGE)
    active = active_applications(page)
    print(f"\nAktive søknader: {len(active)}")
    for row in active:
        print(" ", row)


def cmd_profile(args):
    path = Path(args.laos).expanduser()
    if not path.exists():
        die(EXIT_UNVERIFIED, f"no such file: {path}")
    with portal(headless=not args.headed) as (ctx, page):
        require_session(page, PROFILE_PAGE)
        upload = page.query_selector("input[type=file]")
        if not upload:
            die(EXIT_UNVERIFIED, "no file input on the profile page")
        upload.set_input_files(str(path))
        save = None
        for handle in page.query_selector_all("input[type=submit], button"):
            label = (handle.get_attribute("value") or handle.inner_text() or "").strip()
            if re.search(r"lagre", label, re.I):
                save = handle
                break
        if not save:
            die(EXIT_UNVERIFIED, "no save button on the profile page")
        save.click()
        page.wait_for_load_state("domcontentloaded")
        body = page.inner_text("body")
        if re.search(r"personnummer", body, re.I) and re.search(r"må|mangler|feil", body, re.I):
            die(
                EXIT_UNVERIFIED,
                "the portal refused the save and is asking for a personnummer. "
                "Fill that field yourself in a normal browser, this script will not type it.",
            )
        print("uploaded", path.name)


def main():
    parser = argparse.ArgumentParser(description="Apply to IE læringsassistent courses")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight").set_defaults(func=cmd_preflight)
    login_cmd = sub.add_parser("login")
    login_cmd.add_argument("--user", default=NTNU_USER)
    login_cmd.set_defaults(func=cmd_login)
    sub.add_parser("dump").set_defaults(func=cmd_dump)
    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--campus", choices=("trondheim",))
    list_cmd.set_defaults(func=cmd_list)
    sub.add_parser("status").set_defaults(func=cmd_status)
    apply_cmd = sub.add_parser("apply")
    apply_cmd.add_argument("--all", action="store_true")
    apply_cmd.add_argument("--only")
    apply_cmd.add_argument("--exclude")
    apply_cmd.add_argument("--campus", choices=("trondheim",))
    apply_cmd.add_argument("--submit", action="store_true", help="actually register the applications")
    apply_cmd.set_defaults(func=cmd_apply)
    profile_cmd = sub.add_parser("profile")
    profile_cmd.add_argument("--laos", required=True, help="path to the LAOS kursbevis PDF")
    profile_cmd.set_defaults(func=cmd_profile)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
