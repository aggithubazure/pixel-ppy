"""
Configuration and constants for the Pixel 10 Pro Google One Gemini Bot.
"""

import os

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# ── Device specs – available Pixel device presets ────────────────────────────
# Each preset describes a stock Pixel build that the bot can simulate.  Only
# Pixel models that (in principle) qualify for the Google One / Gemini Pro
# 12-month offer are listed: the Pixel 9 Pro / 9 Pro XL / 9 Pro Fold and the
# Pixel 10 Pro / 10 Pro XL / 10 Pro Fold.  The bot rotates through these across
# retries so each /check_offer attempt presents a different device identity.
DEVICE_PRESETS: dict[str, dict[str, str]] = {
    "pixel_10_pro": {
        "model": "Pixel 10 Pro", "brand": "google", "manufacturer": "Google",
        "android_version": "16", "android_sdk": "36", "build_id": "AP4A.250405.002",
    },
    "pixel_10_pro_xl": {
        "model": "Pixel 10 Pro XL", "brand": "google", "manufacturer": "Google",
        "android_version": "16", "android_sdk": "36", "build_id": "AP4A.250405.003",
    },
    "pixel_10_pro_fold": {
        "model": "Pixel 10 Pro Fold", "brand": "google", "manufacturer": "Google",
        "android_version": "16", "android_sdk": "36", "build_id": "AP4A.250405.004",
    },
    "pixel_9_pro": {
        "model": "Pixel 9 Pro", "brand": "google", "manufacturer": "Google",
        "android_version": "16", "android_sdk": "36", "build_id": "AP4A.250405.005",
    },
    "pixel_9_pro_xl": {
        "model": "Pixel 9 Pro XL", "brand": "google", "manufacturer": "Google",
        "android_version": "16", "android_sdk": "36", "build_id": "AP4A.250405.006",
    },
    "pixel_9_pro_fold": {
        "model": "Pixel 9 Pro Fold", "brand": "google", "manufacturer": "Google",
        "android_version": "16", "android_sdk": "36", "build_id": "AP4A.250405.007",
    },
}

# Active default preset.  Override with the DEVICE_PROFILE environment variable
# (e.g. DEVICE_PROFILE=pixel_9_pro_fold).  The per-attempt rotation in
# check_offer still cycles through every preset regardless of this default.
DEFAULT_DEVICE_PROFILE = "pixel_10_pro"
DEVICE_PROFILE_NAME = (
    os.environ.get("DEVICE_PROFILE", DEFAULT_DEVICE_PROFILE).strip().lower().replace("-", "_")
)
if DEVICE_PROFILE_NAME not in DEVICE_PRESETS:
    DEVICE_PROFILE_NAME = DEFAULT_DEVICE_PROFILE

# Backward-compatible module-level constants point at the active default preset.
_active_device_preset = DEVICE_PRESETS[DEVICE_PROFILE_NAME]
DEVICE_MODEL = _active_device_preset["model"]
DEVICE_BRAND = _active_device_preset["brand"]
DEVICE_MANUFACTURER = _active_device_preset["manufacturer"]
ANDROID_VERSION = _active_device_preset["android_version"]
ANDROID_SDK = _active_device_preset["android_sdk"]
BUILD_ID = _active_device_preset["build_id"]

# ── Auto-detect installed Chrome version ─────────────────────────────────────
# Avoids UA/Client-Hints mismatch with the actual browser binary.
def _detect_chrome_version() -> tuple[str, int]:
    """Detect installed Chrome/Chromium version. Falls back to defaults."""
    import re
    import subprocess
    import shutil

    def _extract_version(text: str) -> tuple[str, int] | None:
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", text or "")
        if not match:
            return None
        version = match.group(1)
        return version, int(version.split(".")[0])

    # Prefer explicitly configured browser path first.
    candidates: list[str] = []
    env_chrome = os.environ.get("CHROME_BIN")
    if env_chrome:
        candidates.append(env_chrome)

    for binary in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        path = shutil.which(binary)
        if path:
            candidates.append(path)

    for path in candidates:
        if not path:
            continue

        # IMPORTANT (Windows): `chrome.exe --version` does NOT print the version
        # to stdout – it launches a visible Chrome window ("New Tab") and never
        # returns the version. Read the version from the executable's file
        # metadata instead and NEVER invoke the browser binary with --version on
        # Windows. (This was the cause of stray "New Tab" Chrome windows.)
        if os.name == "nt":
            if os.path.exists(path):
                try:
                    ps = (
                        "(Get-Item -LiteralPath '" + path.replace("'", "''")
                        + "').VersionInfo.ProductVersion"
                    )
                    out = subprocess.check_output(
                        ["powershell", "-NoProfile", "-Command", ps],
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    ).decode().strip()
                    parsed = _extract_version(out)
                    if parsed:
                        return parsed
                except Exception:
                    pass
            continue  # never fall through to `--version` on Windows

        # POSIX: the browser prints its version to stdout; no window is opened.
        try:
            out = subprocess.check_output(
                [path, "--version"], stderr=subprocess.DEVNULL, timeout=5,
            ).decode().strip()
            parsed = _extract_version(out)
            if parsed:
                return parsed
        except Exception:
            pass

    # Last-resort fallback: use chromedriver version (usually matches browser major).
    for driver_bin in filter(None, (
        os.environ.get("CHROMEDRIVER_PATH"),
        shutil.which("chromedriver"),
    )):
        try:
            out = subprocess.check_output(
                [driver_bin, "--version"], stderr=subprocess.DEVNULL, timeout=5,
            ).decode().strip()
            parsed = _extract_version(out)
            if parsed:
                return parsed
        except Exception:
            continue
    return "124.0.6367.82", 124

CHROME_VERSION, CHROME_MAJOR_VERSION = _detect_chrome_version()

# Pool of realistic Pixel 10 Pro user-agent strings.
# The actual UA is assembled dynamically in device_simulator.py by
# substituting the per-session Chrome version patch suffix.
USER_AGENT_TEMPLATES = [
    (
        "Mozilla/5.0 (Linux; Android {android}; {model} Build/{build}; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Version/4.0 Chrome/{chrome} Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android {android}; {model} Build/{build}) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/{chrome} Mobile Safari/537.36"
    ),
]

# ── Google URLs ───────────────────────────────────────────────────────────────
GMAIL_LOGIN_URL = "https://accounts.google.com/signin/v2/identifier"
GOOGLE_ONE_URL = "https://one.google.com/"
GOOGLE_ONE_OFFERS_URL = "https://one.google.com/about/plans"

# ── Gemini offer detection keywords ──────────────────────────────────────────
GEMINI_OFFER_KEYWORDS = [
    "gemini pro",
    "gemini advanced",
    "12 month",
    "12-month",
    "free trial",
    "activate",
    "get started",
    "claim offer",
    "redeem",
]

# Only accept offer links whose domain matches one of these.
# This prevents generic keywords ("activate", "get started") from
# matching unrelated links on Google pages.
OFFER_DOMAIN_WHITELIST = [
    "one.google.com",
    "gemini.google.com",
    "play.google.com",
    "accounts.google.com",
    "pay.google.com",
]

# ── Selenium / WebDriver ──────────────────────────────────────────────────────
WEBDRIVER_TIMEOUT = 60          # seconds – explicit wait (increased for Google v3 sign-in)
IMPLICIT_WAIT = 10              # seconds
PAGE_LOAD_TIMEOUT = 90          # seconds
HEADLESS = True                 # set to False for local debugging with visible browser

# ── Email validation ──────────────────────────────────────────────────────────
# Leave empty to accept any valid email domain (Gmail + Google Workspace).
# Populate with specific domains to restrict, e.g. ["gmail.com", "mycompany.com"]
ALLOWED_EMAIL_DOMAINS: list[str] = []

# ── Session ───────────────────────────────────────────────────────────────────
# Session time-to-live in seconds.  After this period the session
# (including any stored credentials) is automatically purged.
SESSION_TTL_SECONDS: int = 30 * 60   # 30 minutes

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
