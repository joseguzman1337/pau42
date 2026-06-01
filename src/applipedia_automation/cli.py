from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page, TimeoutError, async_playwright


BASE_URL = "https://applipedia.paloaltonetworks.com"
LOGIN_URL = f"{BASE_URL}/api/auth/login/?next=%2F%3Fsearch%3D"
DEFAULT_STATE_DIR = Path(".auth/applipedia")
DEFAULT_TIMEOUT_MS = 30_000
APP_FIELD_MAP = {
    "Application Name": ("product_name", "application_name", "name"),
    "Category": ("new_category", "category"),
    "Subcategory": ("subcategory",),
    "App-ID Type": ("content_type", "app_id_type", "appid_type"),
    "Risk": ("risk",),
    "App-ID Name": ("appid", "app_id", "app_id_name", "new_appid"),
    "DLP Supported": ("dlp_support", "dlp_supported"),
    "Requires Decryption": ("need_decryption", "requires_decryption"),
}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


async def get_me(context: BrowserContext) -> dict[str, Any]:
    response = await context.request.get(f"{BASE_URL}/api/v1/me/")
    if not response.ok:
        raise RuntimeError(f"Applipedia /me check failed with HTTP {response.status}: {await response.text()}")
    payload = await response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected /me response: {payload!r}")
    return payload


async def is_authenticated(context: BrowserContext) -> bool:
    return bool((await get_me(context)).get("is_authenticated"))


async def fill_first(page: Page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=5_000)
            await locator.fill(value)
            return True
        except TimeoutError:
            continue
    return False


async def click_first(page: Page, selectors: list[str]) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=5_000)
            await locator.click()
            return True
        except TimeoutError:
            continue
    return False


def resolve_credentials(args: argparse.Namespace) -> tuple[str | None, str | None]:
    username = args.username or os.getenv("APPLIPEDIA_USERNAME")
    password = os.getenv("APPLIPEDIA_PASSWORD")

    if not args.prompt_credentials:
        return username, password

    if not sys.stdin.isatty():
        raise RuntimeError("--prompt-credentials requires an interactive terminal.")

    if not username:
        username = input("Applipedia email: ").strip()
    if not password:
        password = getpass.getpass("Applipedia password: ")

    return username or None, password or None


async def try_credential_login(page: Page, username: str | None, password: str | None) -> None:

    if username:
        filled = await fill_first(
            page,
            [
                "input[name='identifier']",
                "input#input28",
                "input[type='email']",
                "input[type='text']",
                "input[autocomplete='username']",
            ],
            username,
        )
        if filled:
            await click_first(page, ["input[type='submit']", "button:has-text('Next')", "text=Next"])
            await page.wait_for_load_state("domcontentloaded")

    if password:
        filled = await fill_first(
            page,
            [
                "input[type='password']",
                "input[name='credentials.passcode']",
                "input[autocomplete='current-password']",
            ],
            password,
        )
        if filled:
            await click_first(
                page,
                [
                    "input[type='submit']",
                    "button:has-text('Verify')",
                    "button:has-text('Sign In')",
                    "button:has-text('Log In')",
                ],
            )
            await page.wait_for_load_state("domcontentloaded")


async def ensure_login(
    context: BrowserContext,
    *,
    headless: bool,
    timeout_seconds: int,
    username: str | None,
    password: str | None,
) -> None:
    if await is_authenticated(context):
        return

    page = await context.new_page()
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await try_credential_login(page, username, password)

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if await is_authenticated(context):
            return
        if headless:
            break
        print(
            "Waiting for browser login. Complete Okta/MFA/CAPTCHA in the opened window...",
            file=sys.stderr,
            flush=True,
        )
        await page.wait_for_timeout(3_000)

    raise RuntimeError(
        "Not authenticated. Run with --headed and complete the Palo Alto Okta flow, "
        "or pass --prompt-credentials / set APPLIPEDIA_USERNAME and APPLIPEDIA_PASSWORD for the non-MFA parts."
    )


def is_query_response(url: str, search: str, offset: int, limit: int) -> bool:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return (
        parsed.path == "/api/v1/query/"
        and query.get("search", [""])[0] == search
        and query.get("offset", ["0"])[0] == str(offset)
        and query.get("limit", ["25"])[0] == str(limit)
    )


async def search_via_portal(context: BrowserContext, search: str, *, offset: int, limit: int) -> Any:
    page = context.pages[0] if context.pages else await context.new_page()
    params = urlencode({"search": search, "offset": offset, "limit": limit})
    async with page.expect_response(
        lambda response: is_query_response(response.url, search, offset, limit),
        timeout=DEFAULT_TIMEOUT_MS,
    ) as response_info:
        await page.goto(f"{BASE_URL}/?{params}", wait_until="domcontentloaded")
    response = await response_info.value
    body = await response.text()
    if not response.ok:
        hint = ""
        if "recaptcha" in body.lower():
            hint = " Run with --headed so the portal can produce a valid reCAPTCHA action token."
        raise RuntimeError(f"Search failed with HTTP {response.status}: {body}{hint}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Search returned non-JSON response: {body[:500]}") from error


def unwrap_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "data", "items", "applications", "rows"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return "; ".join(format_value(item) for item in value)
    if isinstance(value, dict):
        for key in ("display_name", "name", "value", "label"):
            if key in value:
                return format_value(value[key])
        return json.dumps(value, sort_keys=True)
    return str(value)


def get_first_value(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in row:
            return format_value(row[key])
    return ""


def normalize_app_row(row: dict[str, Any]) -> dict[str, str]:
    return {header: get_first_value(row, keys) for header, keys in APP_FIELD_MAP.items()}


def print_table(rows: list[dict[str, Any]]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=list(APP_FIELD_MAP.keys()))
    writer.writeheader()
    writer.writerows(normalize_app_row(row) for row in rows)


async def run(args: argparse.Namespace) -> None:
    state_dir = Path(args.state_dir).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    username, password = resolve_credentials(args)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(state_dir),
            headless=args.headless,
            viewport={"width": 1280, "height": 900},
            timeout=DEFAULT_TIMEOUT_MS,
        )
        try:
            if args.command == "login":
                await ensure_login(
                    context,
                    headless=args.headless,
                    timeout_seconds=args.login_timeout,
                    username=username,
                    password=password,
                )
                print(json.dumps(await get_me(context), indent=2, sort_keys=True))
                return

            if args.command == "whoami":
                print(json.dumps(await get_me(context), indent=2, sort_keys=True))
                return

            if args.command == "search":
                if args.login:
                    await ensure_login(
                        context,
                        headless=args.headless,
                        timeout_seconds=args.login_timeout,
                        username=username,
                        password=password,
                    )
                payload = await search_via_portal(context, args.term, offset=args.offset, limit=args.limit)
                if args.format == "json":
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print_table(unwrap_results(payload))
                return

            raise RuntimeError(f"Unsupported command: {args.command}")
        finally:
            await context.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Automate Palo Alto Networks Applipedia login and search.")
    parser.add_argument("--state-dir", default=os.getenv("APPLIPEDIA_STATE_DIR", str(DEFAULT_STATE_DIR)))
    parser.add_argument("--headed", dest="headless", action="store_false", help="Open a visible browser for Okta/MFA.")
    parser.add_argument("--headless", dest="headless", action="store_true", help="Run Chromium headless.")
    parser.add_argument("--username", help="Applipedia email. Defaults to APPLIPEDIA_USERNAME.")
    parser.add_argument(
        "--prompt-credentials",
        action="store_true",
        help="Prompt for missing email/password at runtime. Password input is masked.",
    )
    parser.set_defaults(headless=env_bool("APPLIPEDIA_HEADLESS", False))
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="Open/login and persist the browser session.")
    login.add_argument("--login-timeout", type=int, default=300)

    subparsers.add_parser("whoami", help="Show current Applipedia authentication status.")

    search = subparsers.add_parser("search", help="Search Applipedia applications.")
    search.add_argument("term")
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--limit", type=int, default=25)
    search.add_argument("--format", choices=["json", "csv"], default="json")
    search.add_argument("--no-login", dest="login", action="store_false", help="Do not attempt login before search.")
    search.add_argument("--login-timeout", type=int, default=300)
    search.set_defaults(login=True)
    return parser


def main() -> None:
    load_dotenv()
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
