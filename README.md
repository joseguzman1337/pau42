# Applipedia automation

Programmatic access to Palo Alto Networks Applipedia with uv, Python, and Playwright.

The site uses Palo Alto SSO through Okta/SAML. This script automates the repeatable parts, persists the browser profile in `.auth/applipedia`, and lets you finish MFA, CAPTCHA, or other policy checks in a headed browser when required.

## Setup

```bash
uv sync
uv run playwright install chromium
cp .env.example .env
```

Set `APPLIPEDIA_USERNAME` and optionally `APPLIPEDIA_PASSWORD` in `.env`. Leave the password blank if your account requires MFA or if you prefer to type it into the browser.

You can also avoid saving credentials and enter them at runtime:

```bash
uv run applipedia --headed --prompt-credentials login
uv run applipedia --headed --username you@example.com --prompt-credentials login
```

`--prompt-credentials` asks for any missing email/password values in the terminal. Password input is masked and is not written to `.env`.

## Login

```bash
uv run applipedia --headed login
```

Complete the Okta flow in the opened browser. The authenticated session is saved locally under `.auth/applipedia`.

## Search

```bash
uv run applipedia search ssl --format json
uv run applipedia --headless search zoom --format csv --limit 50
uv run applipedia whoami
```

The search command calls:

```text
https://applipedia.paloaltonetworks.com/api/v1/query/?search=<term>&offset=0&limit=25
```

If the saved session has expired, run the headed login command again.
