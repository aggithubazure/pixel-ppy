"""
Google One automation using Selenium.

Logs into a Google account (Gmail or Google Workspace), navigates to
Google One, detects the 12-month free Gemini Pro offer, and returns
the activation / payment link.
"""

import logging
import os
import time
import re
from urllib.parse import urlparse
from typing import Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

import config
from device_simulator import DeviceProfile

logger = logging.getLogger(__name__)


# ── Driver factory ────────────────────────────────────────────────────────────
def _classify_login_error_message(error_text: str) -> str:
    """Classify known Google sign-in error messages."""
    text = (error_text or "").lower()
    if any(k in text for k in (
        "wrong password",
        "couldn’t find your google account",
        "couldn't find your google account",
        "enter a valid email",
        "enter an email or phone number",
        "incorrect password",
    )):
        return "invalid_credentials"
    if any(k in text for k in (
        "try again later",
        "unusual activity",
        "couldn't sign you in",
        "this browser or app may not be secure",
        "verify it’s you",
        "verify it's you",
    )):
        return "google_challenge"
    return "failed"


def _ensure_chromium_installed() -> tuple[str, str]:
    """Find Chromium and chromedriver.  Returns (chrome_bin, chromedriver_path).

    Raises GoogleAutomationError if either cannot be found.
    """
    import shutil

    # Check environment variables first, then system PATH
    chrome_bin = (os.environ.get("CHROME_BIN")
                  or shutil.which("chromium")
                  or shutil.which("chromium-browser")
                  or shutil.which("google-chrome"))

    chromedriver_path = (os.environ.get("CHROMEDRIVER_PATH")
                         or shutil.which("chromedriver"))

    if not chrome_bin:
        raise GoogleAutomationError(
            "Chromium is not installed. "
            "Set CHROME_BIN env var or install chromium."
        )
    if not chromedriver_path:
        raise GoogleAutomationError(
            "chromedriver is not installed. "
            "Set CHROMEDRIVER_PATH env var or install chromedriver."
        )

    return chrome_bin, chromedriver_path


def _build_driver(profile: DeviceProfile) -> webdriver.Chrome:
    """Return a headless Chrome WebDriver configured for the device profile."""
    from device_simulator import PIXEL_10_PRO_SPECS as SPECS

    options = Options()

    if config.HEADLESS:
        # Use new headless mode (Chrome 112+); old --headless breaks JS rendering
        # on Google Sign-in v3 and causes timeout on input[type="email"].
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument(f"--window-size={SPECS['width']},{SPECS['height']}")
    options.add_argument(f"--user-agent={profile.user_agent}")

    # ── Stability flags ──────────────────────────────────────────────────────
    # NOTE: --disable-features=VizDisplayCompositor removed — breaks rendering
    # in new headless mode and can prevent Google sign-in form from appearing.
    # NOTE: --renderer-process-limit removed — can cause Chrome process crashes.
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-crash-reporter")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-translate")
    options.add_argument("--no-first-run")
    options.add_argument("--js-flags=--max-old-space-size=512")
    options.add_argument("--disable-ipc-flooding-protection")
    options.add_argument("--disable-popup-blocking")

    # ── Locate Chrome/Chromium and chromedriver ───────────────────────────
    chrome_bin, chromedriver_path = _ensure_chromium_installed()

    if chrome_bin:
        options.binary_location = chrome_bin
        logger.info("Using Chrome binary: %s", chrome_bin)
    else:
        logger.warning("No Chrome/Chromium found – driver may fail to start.")

    # Mobile emulation – Pixel 10 Pro viewport
    mobile_emulation = {
        "deviceMetrics": {
            "width": SPECS["width"],
            "height": SPECS["height"],
            "pixelRatio": SPECS["pixel_ratio"],
            "mobile": True,
            "touch": True,
        },
        "userAgent": profile.user_agent,
    }
    options.add_experimental_option("mobileEmulation", mobile_emulation)

    # Suppress automation flags
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--disable-blink-features=AutomationControlled")

    # ── Create driver ─────────────────────────────────────────────────────
    if chromedriver_path:
        logger.info("Using chromedriver: %s", chromedriver_path)
        service = Service(chromedriver_path)
        driver = webdriver.Chrome(service=service, options=options)
    else:
        logger.warning("No chromedriver found – using Selenium manager fallback.")
        driver = webdriver.Chrome(options=options)

    driver.implicitly_wait(config.IMPLICIT_WAIT)
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)

    # ── Inject navigator/WebGL/screen overrides via CDP ───────────────────
    try:
        # Inject JS spoofs on every page load
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": profile.navigator_overrides_js()},
        )

        # Set Client Hints and device headers at network level
        driver.execute_cdp_cmd(
            "Network.setExtraHTTPHeaders",
            {"headers": profile.as_headers()},
        )

        # Enable touch emulation
        driver.execute_cdp_cmd(
            "Emulation.setTouchEmulationEnabled",
            {"enabled": True, "maxTouchPoints": SPECS["max_touch_points"]},
        )

        # Set timezone to US Pacific (most Pixel offers are US)
        driver.execute_cdp_cmd(
            "Emulation.setTimezoneOverride",
            {"timezoneId": "America/Los_Angeles"},
        )

        # Set geolocation to Mountain View, CA (Google HQ area)
        driver.execute_cdp_cmd(
            "Emulation.setGeolocationOverride",
            {
                "latitude": 37.3861,
                "longitude": -122.0839,
                "accuracy": 100,
            },
        )

        logger.info(
            "Device emulation configured: %s (Build %s, Chrome %s)",
            profile.model, profile.build_id, profile.chrome_version,
        )
    except Exception as exc:
        logger.warning("CDP override injection failed (non-fatal): %s", exc)

    return driver


# ── Login helper ──────────────────────────────────────────────────────────────

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def _save_debug_screenshot(driver: webdriver.Chrome, label: str) -> None:
    """Save a screenshot to /app/logs/ for debugging failed page states."""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        filename = os.path.join(
            _LOG_DIR,
            f"debug_{label}_{int(time.time())}.png",
        )
        driver.save_screenshot(filename)
        logger.info("Debug screenshot saved: %s", filename)
    except Exception as exc:
        logger.warning("Could not save debug screenshot: %s", exc)


def _wait_for(driver: webdriver.Chrome, by: str, value: str,
               timeout: int = config.WEBDRIVER_TIMEOUT) -> WebElement:
    """Return element after waiting for it to be clickable."""
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )


def _gmail_login(driver: webdriver.Chrome, email: str, password: str) -> str:
    """
    Perform Gmail / Google account login.

    Returns:
        "success"             – login completed
        "needs_totp"          – TOTP / authenticator code required
        "invalid_credentials" – Google rejected email/password
        "google_challenge"    – blocked/challenge flow incompatible with current run
        "timeout"             – page interaction timed out
        "webdriver_crashed"   – browser session disconnected/crashed
        "failed"              – unclassified login failure
    Raises GoogleAutomationError for unsupported 2FA types.
    """
    try:
        driver.implicitly_wait(0)  # Prevent find_element from blocking
        driver.get(config.GMAIL_LOGIN_URL)

        # Wait for page to be fully interactive before looking for elements
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(2)  # Allow injected JS to settle after readyState complete

        logger.info("Login page loaded – title: %r, URL: %s",
                    driver.title, driver.current_url)

        # ── Email step ────────────────────────────────────────────────────────
        # Try multiple selectors: v3 uses #identifierId; v2 uses input[type="email"]
        _email_selectors = [
            (By.ID, "identifierId"),
            (By.CSS_SELECTOR, 'input[type="email"]'),
            (By.CSS_SELECTOR, 'input[name="identifier"]'),
        ]
        email_field = None
        for _retry in range(3):
            for by, selector in _email_selectors:
                try:
                    email_field = WebDriverWait(driver, 20).until(
                        EC.element_to_be_clickable((by, selector))
                    )
                    logger.info("Email field found with selector: %s %r", by, selector)
                    break
                except (TimeoutException, NoSuchElementException):
                    continue
            if email_field:
                break
            try:
                email_field.clear()
            except Exception:
                pass
            logger.warning("Email field not found on attempt %d/3, retrying…", _retry + 1)
            time.sleep(1)
        else:
            # Save screenshot so we can see what Google is actually showing
            _save_debug_screenshot(driver, "email_field_not_found")
            logger.error(
                "Email field not found after 3 attempts. "
                "Page title: %r | URL: %s | Source snippet: %s",
                driver.title,
                driver.current_url,
                driver.page_source[:500],
            )
            raise GoogleAutomationError(
                "Could not find the Google sign-in email input. "
                "Google may be showing a bot-detection/challenge page. "
                "A screenshot was saved to /app/logs/ for inspection.",
                code="email_field_missing",
            )

        try:
            email_field.clear()
            email_field.send_keys(email)
        except StaleElementReferenceException:
            logger.warning("Stale element on email field, retrying send_keys")
            time.sleep(1)
            email_field = driver.find_element(By.ID, "identifierId")
            email_field.clear()
            email_field.send_keys(email)

        next_btn = _wait_for(driver, By.ID, "identifierNext")
        next_btn.click()
        time.sleep(1)

        # ── Password step ─────────────────────────────────────────────────────
        password_field = _wait_for(driver, By.CSS_SELECTOR,
                                   'input[type="password"]')
        password_field.clear()
        password_field.send_keys(password)

        pw_next = _wait_for(driver, By.ID, "passwordNext")
        pw_next.click()
        time.sleep(2)

        # ── Detect 2FA / verification challenges ─────────────────────────────
        current_url = driver.current_url
        parsed = urlparse(current_url)
        hostname = parsed.hostname or ""
        path = parsed.path or ""

        # Known 2FA challenge URL patterns
        _2fa_path_patterns = (
            "/signin/v2/challenge",   # general challenge page
            "/signin/challenge",      # alternate challenge path
            "/v2/challenge",          # short variant
        )

        if hostname == "accounts.google.com" and any(
            p in path for p in _2fa_path_patterns
        ):
            page_text = driver.page_source.lower()

            # TOTP / Authenticator → check if the input field is actually present
            _totp_input_selectors = (
                'input[type="tel"]',
                'input[name="totpPin"]',
                '#totpPin',
            )
            has_totp_input = False
            for sel in _totp_input_selectors:
                try:
                    driver.find_element(By.CSS_SELECTOR, sel)
                    has_totp_input = True
                    break
                except NoSuchElementException:
                    continue

            if has_totp_input:
                logger.info("TOTP 2FA input field found for %s – awaiting code", email)
                return "needs_totp"

            # Not showing TOTP input directly – try to navigate to it
            switched_to_totp = False

            try:
                # Step 1: Try selecting TOTP directly on the page
                # (works when already on /challenge/selection)
                for opt_xpath in (
                    '//*[@data-challengetype="6"]',    # TOTP challenge type
                    '//div[@data-challengetype="6"]',
                    '//div[contains(text(), "Authenticator")]',
                    '//div[contains(text(), "authenticator")]',
                    '//div[contains(text(), "Google Authenticator")]',
                    '//div[contains(text(), "verification code")]',
                    '//li[contains(., "Authenticator")]',
                    '//li[contains(., "authenticator")]',
                ):
                    try:
                        opt = driver.find_element(By.XPATH, opt_xpath)
                        opt.click()
                        time.sleep(2)
                        switched_to_totp = True
                        logger.info("Selected authenticator option directly for %s", email)
                        break
                    except NoSuchElementException:
                        continue

                # Step 2: If not found, try clicking "Try another way" first
                if not switched_to_totp:
                    try_another = None
                    for selector in (
                        '//a[contains(text(), "another way")]',
                        '//button[contains(text(), "another way")]',
                        '//a[contains(text(), "other way")]',
                        '//a[contains(text(), "Try another")]',
                        '//span[contains(text(), "another way")]/ancestor::a',
                        '//span[contains(text(), "another way")]/ancestor::button',
                    ):
                        try:
                            try_another = driver.find_element(By.XPATH, selector)
                            if try_another:
                                break
                        except NoSuchElementException:
                            continue

                    if try_another:
                        try_another.click()
                        time.sleep(2)
                        logger.info("Clicked 'Try another way' for %s", email)

                        # Now look for authenticator / TOTP option
                        for opt_xpath in (
                            '//*[@data-challengetype="6"]',
                            '//div[@data-challengetype="6"]',
                            '//div[contains(text(), "Authenticator")]',
                            '//div[contains(text(), "authenticator")]',
                            '//div[contains(text(), "Google Authenticator")]',
                            '//div[contains(text(), "verification code")]',
                            '//li[contains(., "Authenticator")]',
                        ):
                            try:
                                opt = driver.find_element(By.XPATH, opt_xpath)
                                opt.click()
                                time.sleep(1)
                                switched_to_totp = True
                                logger.info("Selected authenticator option for %s", email)
                                break
                            except NoSuchElementException:
                                continue

                if switched_to_totp:
                    return "needs_totp"

                # Check if the page now shows TOTP input after navigation
                for sel in _totp_input_selectors:
                    try:
                        driver.find_element(By.CSS_SELECTOR, sel)
                        return "needs_totp"
                    except NoSuchElementException:
                        continue

            except Exception as exc:
                logger.warning("Error trying alternative 2FA: %s", exc)

            # No TOTP option found → raise error with detailed guidance
            page_text = driver.page_source.lower()
            if "security key" in page_text or "usb" in page_text or "/challenge/sk" in current_url:
                challenge_type = "security key / passkey"
                guidance = (
                    "Your account uses a hardware security key (passkey) as 2FA. "
                    "The bot cannot use hardware keys.\n\n"
                    "✅ Solution: Create an App Password at "
                    "https://myaccount.google.com/apppasswords "
                    "and use it instead of your Gmail password in /login."
                )
            elif "phone" in page_text or "sms" in page_text:
                challenge_type = "SMS / phone verification"
                guidance = (
                    "Your account uses SMS as 2FA. "
                    "✅ Solution: Create an App Password at "
                    "https://myaccount.google.com/apppasswords "
                    "and use it instead of your Gmail password in /login."
                )
            elif "tap yes" in page_text or "google prompt" in page_text:
                challenge_type = "Google prompt (tap Yes on your phone)"
                guidance = (
                    "Your account uses Google prompt 2FA. "
                    "✅ Solution: Create an App Password at "
                    "https://myaccount.google.com/apppasswords "
                    "and use it instead of your Gmail password in /login."
                )
            else:
                challenge_type = "two-step verification"
                guidance = (
                    "✅ Solution: Create an App Password at "
                    "https://myaccount.google.com/apppasswords "
                    "and use it instead of your Gmail password in /login."
                )

            logger.warning(
                "Unsupported 2FA for %s: %s (URL: %s)",
                email, challenge_type, current_url,
            )
            raise GoogleAutomationError(
                f"2FA required: {challenge_type}\n\n{guidance}",
                code="unsupported_2fa",
            )

        # ── Verify login ──────────────────────────────────────────────────────
        if (
            hostname == "myaccount.google.com"
            or (hostname.endswith(".google.com") and "/u/" in path)
        ):
            logger.info("Login succeeded for %s", email)
            return "success"

        # Check for error messages
        try:
            error_el = driver.find_element(
                By.CSS_SELECTOR, '[jsname="B34EJ"], [aria-live="assertive"]'
            )
            if error_el.text:
                logger.warning("Login error detected: %s", error_el.text)
                return _classify_login_error_message(error_el.text)
        except NoSuchElementException:
            pass

        # If we're no longer on the login page, assume success
        if not (
            hostname == "accounts.google.com"
            and path.startswith("/signin")
        ):
            logger.info("Login appeared successful for %s (URL: %s)",
                        email, current_url)
            return "success"

        logger.warning("Unexpected URL after login: %s", current_url)
        return "failed"

    except TimeoutException as exc:
        # Save screenshot so we can inspect what Google actually showed
        _save_debug_screenshot(driver, "login_timeout")
        try:
            page_info = f"title={driver.title!r} url={driver.current_url} src={driver.page_source[:300]}"
        except Exception:
            page_info = "(driver unresponsive)"
        logger.error("Timeout during login – %s | Exception: %s", page_info, exc)
        return "timeout"
    except WebDriverException as exc:
        logger.error("WebDriver error during login: %s", exc)
        msg = str(exc).lower()
        if any(k in msg for k in ("connection refused", "disconnected", "invalid session id")):
            return "webdriver_crashed"
        return "failed"


def _submit_totp_code(driver: webdriver.Chrome, code: str) -> bool:
    """Enter a TOTP / authenticator code on the 2FA challenge page.

    Returns True if the code was accepted and login completed.
    """
    try:
        # Find the TOTP input field
        totp_field = None
        for selector in (
            'input[type="tel"]',           # Most common – numeric input
            'input[name="totpPin"]',       # Direct name
            '#totpPin',
            'input[type="text"]',          # Fallback
        ):
            try:
                totp_field = WebDriverWait(driver, 2).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                if totp_field:
                    break
            except TimeoutException:
                continue

        if not totp_field:
            logger.error("Could not find TOTP input field")
            return False

        totp_field.clear()
        totp_field.send_keys(code)
        time.sleep(0.5)

        # Click Next / Verify button
        for btn_selector in (
            '#totpNext',
            'button[jsname="LgbsSe"]',
            '[data-action="verify"]',
            'button[type="submit"]',
        ):
            try:
                btn = driver.find_element(By.CSS_SELECTOR, btn_selector)
                btn.click()
                break
            except NoSuchElementException:
                continue

        time.sleep(2)

        # Check if we left the challenge page
        current_url = driver.current_url
        parsed = urlparse(current_url)
        hostname = parsed.hostname or ""
        path = parsed.path or ""

        if hostname == "accounts.google.com" and "challenge" in path:
            logger.warning("Still on challenge page after TOTP – code may be wrong")
            return False

        logger.info("TOTP accepted, login completed")
        return True

    except Exception as exc:
        logger.error("Error submitting TOTP code: %s", exc)
        return False


# ── Offer detection ───────────────────────────────────────────────────────────


def _is_valid_offer_url(href: str) -> bool:
    """Return True if *href* belongs to a whitelisted offer domain.

    When ``config.OFFER_DOMAIN_WHITELIST`` is empty every URL is accepted.
    """
    if not href:
        return False
    whitelist = config.OFFER_DOMAIN_WHITELIST
    if not whitelist:
        return bool(href)
    try:
        hostname = urlparse(href).hostname or ""
        return any(
            hostname == d or hostname.endswith("." + d)
            for d in whitelist
        )
    except Exception:
        return False


def _is_correct_offer_url(url: str) -> bool:
    """Return True if *url* is a valid Pixel Gemini Pro offer claim URL.

    The correct offer URL format is:
        https://one.google.com/partner-eft-onboard/XXXXXXX
    """
    if not url:
        return False
    return "partner-eft-onboard" in url


def _extract_payment_link(driver: webdriver.Chrome) -> Optional[str]:
    """
    Scan the current page for a Gemini Pro offer / activation link.

    The correct offer URL contains ``partner-eft-onboard``.
    Strategy 0 clicks the LOCKED benefit link and validates the result.
    Strategies 1-3 only accept non-LOCKED URLs.
    """
    # ── Page content validation keywords ────────────────────────────────────
    _OFFER_PAGE_KEYWORDS = [
        "ai premium",
        "gemini advanced",
        "gemini pro",
        "12 month",
        "12-month",
        "start free trial",
        "start trial",
        "subscribe",
        "free trial",
        "google one ai",
        "premium plan",
        "partner-eft-onboard",
    ]

    def _page_has_offer_content(page_text: str) -> bool:
        """Check if page text contains at least 2 offer-related keywords."""
        text = page_text.lower()
        matches = sum(1 for kw in _OFFER_PAGE_KEYWORDS if kw in text)
        return matches >= 2

    # -- Strategy 0: Click LOCKED benefit to navigate to claim page -----------
    all_links = driver.find_elements(By.TAG_NAME, "a")
    for link in all_links:
        try:
            href = link.get_attribute("href") or ""
            if "LOCKED" in href and "BARD_ADVANCED" in href:
                logger.info("Found LOCKED benefit link: %s", href)
                old_url = driver.current_url

                # Use JavaScript click to bypass overlay elements
                driver.execute_script("arguments[0].click();", link)
                time.sleep(5)

                current_url = driver.current_url
                logger.info("After clicking LOCKED link, URL: %s", current_url)

                # Best case: URL contains partner-eft-onboard
                if _is_correct_offer_url(current_url):
                    logger.info("✅ Found correct offer URL: %s", current_url)
                    return current_url

                # If URL still contains LOCKED, the page didn't navigate
                # to the real claim page — device doesn't qualify
                if "LOCKED" in current_url:
                    logger.warning(
                        "URL still contains LOCKED after click (%s). "
                        "Device does not qualify for offer.",
                        current_url,
                    )
                    return None  # Trigger retry

                # Page navigated to non-LOCKED URL — scan for partner-eft-onboard
                if current_url != old_url:
                    # Scan new page for partner-eft-onboard links
                    new_links = driver.find_elements(By.TAG_NAME, "a")
                    for nl in new_links:
                        try:
                            nh = nl.get_attribute("href") or ""
                            if _is_correct_offer_url(nh):
                                logger.info("✅ Found partner-eft-onboard link on page: %s", nh)
                                return nh
                        except Exception:
                            continue

                    # Also check if current URL itself is partner-eft-onboard
                    if _is_correct_offer_url(current_url):
                        logger.info("✅ Current URL is partner-eft-onboard: %s", current_url)
                        return current_url

                    logger.warning(
                        "Page navigated to %s but no partner-eft-onboard link found",
                        current_url,
                    )
                else:
                    logger.warning(
                        "LOCKED link click did not navigate (still %s). "
                        "Device may not qualify.",
                        current_url,
                    )

                # Return None to trigger retry with new device
                return None
        except Exception as exc:
            logger.warning("Error clicking LOCKED link: %s", exc)
            # Click failed — return None to trigger retry
            return None

    # -- Strategy 1: scan for partner-eft-onboard links directly ---------------
    for link in all_links:
        try:
            href = link.get_attribute("href") or ""
            if _is_correct_offer_url(href):
                logger.info("Found partner-eft-onboard link: %s", href)
                return href
        except Exception:
            continue

    # -- Strategy 2: anchor text / aria-label match → only partner-eft-onboard --
    keywords = config.GEMINI_OFFER_KEYWORDS
    for link in all_links:
        try:
            text = (link.text + " " + (link.get_attribute("aria-label") or "")).lower()
            href = link.get_attribute("href") or ""
            if "LOCKED" in href:
                continue  # Skip LOCKED URLs
            if any(kw in text for kw in keywords) and _is_correct_offer_url(href):
                logger.info("Found partner-eft-onboard link via text match: %s", href)
                return href
        except Exception:
            continue

    # -- Strategy 3: broad URL scan → only return partner-eft-onboard ----------
    for link in all_links:
        try:
            href = link.get_attribute("href") or ""
            if _is_correct_offer_url(href):
                logger.info("Found partner-eft-onboard link via broad scan: %s", href)
                return href
        except Exception:
            continue

    return None


def _navigate_google_one(driver: webdriver.Chrome) -> Optional[str]:
    """
    Navigate to Google One and attempt to find the Gemini Pro offer link.

    Returns the payment/activation URL or None if not found.
    """
    for url in (config.GOOGLE_ONE_URL, config.GOOGLE_ONE_OFFERS_URL):
        try:
            logger.info("Navigating to %s", url)
            driver.get(url)
            time.sleep(3)

            # Dismiss cookie/consent banners if present
            for selector in (
                '[aria-label="Accept all"]',
                'button[jsname="higCR"]',
                '[data-action="accept"]',
            ):
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, selector)
                    btn.click()
                    time.sleep(1)
                    break
                except NoSuchElementException:
                    pass

            link = _extract_payment_link(driver)
            if link:
                return link

        except (TimeoutException, WebDriverException) as exc:
            logger.warning("Error accessing %s: %s", url, exc)

    return None


# ── Public API ────────────────────────────────────────────────────────────────

class GoogleAutomationError(Exception):
    """Raised when automation encounters an unrecoverable error."""
    def __init__(self, message: str, code: str = "automation_error"):
        super().__init__(message)
        self.code = code


def start_login(email: str, password: str,
                device: DeviceProfile) -> tuple:
    """
    Start the login process.

    Returns (driver, status) where status is:
        "success"    – login completed, ready for offer check
        "needs_totp" – TOTP code needed, driver is on 2FA page

    The caller is responsible for calling driver.quit() when done.
    Raises GoogleAutomationError on startup or unsupported 2FA.
    """
    logger.info("Starting WebDriver for session %s", device.session_id)
    driver = _build_driver(device)

    try:
        status = _gmail_login(driver, email, password)
        if status == "invalid_credentials":
            driver.quit()
            raise GoogleAutomationError(
                "Google rejected your email/password. Please re-check credentials or use an App Password if 2FA is enabled.",
                code="invalid_credentials",
            )
        if status == "google_challenge":
            driver.quit()
            raise GoogleAutomationError(
                "Google blocked this automated sign-in (security challenge/risk check). Try confirming the login manually on the same server IP, then run /check_offer again.",
                code="google_challenge",
            )
        if status == "timeout":
            driver.quit()
            raise GoogleAutomationError(
                "Login timed out while waiting for Google sign-in elements. This is usually page/challenge latency rather than wrong credentials.",
                code="login_timeout",
            )
        if status == "webdriver_crashed":
            driver.quit()
            raise GoogleAutomationError(
                "Browser session crashed/disconnected during login (WebDriver connection refused). Check Chromium stability and container limits.",
                code="webdriver_crashed",
            )
        if status == "failed":
            driver.quit()
            raise GoogleAutomationError(
                "Login failed at an unexpected step (not a clear credential error). Check server logs for the exact Google page state.",
                code="login_unknown",
            )
        return driver, status
    except GoogleAutomationError:
        driver.quit()
        raise
    except Exception:
        driver.quit()
        raise


def submit_2fa_code(driver, code: str) -> bool:
    """Submit a TOTP code on a driver that is on the 2FA challenge page.

    Returns True if the code was accepted.
    """
    return _submit_totp_code(driver, code)


def check_offer_with_driver(driver) -> Optional[str]:
    """Navigate to Google One and find the Gemini Pro offer link.

    Returns the offer URL or None.
    """
    return _navigate_google_one(driver)


def close_driver(driver) -> None:
    """Safely close the WebDriver."""
    if driver:
        try:
            driver.quit()
        except Exception:
            pass
