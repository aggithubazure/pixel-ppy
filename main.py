"""
Telegram Bot entry point for the Pixel 10 Pro Google One Gemini Bot.

Commands:
  /start        – Show welcome message and available commands
  /login        – Begin credential capture flow (email → password)
  /logout       – Clear stored credentials and session data
  /check_offer  – Run Google One automation and look for Gemini Pro offer
  /get_link     – Show the last captured offer link
  /status       – Show current session status and device profile

Supports both Gmail (user@gmail.com) and Google Workspace (user@company.com)
accounts.
"""

import asyncio
import logging
import os
import random
import re
import sys
import time

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
from device_simulator import create_device_profile, ALL_DEVICE_PROFILES
from google_automation import (
    GoogleAutomationError,
    start_login,
    submit_2fa_code,
    TOTP_LOCKED,
    check_offer_with_driver,
    close_driver,
)

# ── Logging ───────────────────────────────────────────────────────────────────
from datetime import datetime as _dt

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

_formatter = logging.Formatter(config.LOG_FORMAT)

# Console handler
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

# File handler – new file per startup: bot_YYYYMMDD_HHMMSS.log
_log_filename = f"bot_{_dt.now().strftime('%Y%m%d_%H%M%S')}.log"
_file_handler = logging.FileHandler(
    os.path.join(_LOG_DIR, _log_filename),
    encoding="utf-8",
)
_file_handler.setFormatter(_formatter)

logging.basicConfig(
    level=config.LOG_LEVEL,
    handlers=[_console_handler, _file_handler],
)
logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
AWAIT_EMAIL, AWAIT_PASSWORD = range(2)
AWAIT_2FA_CODE = 10  # Separate state for 2FA code input
MAX_2FA_ATTEMPTS = 3

# ── Rate limiting & concurrency ───────────────────────────────────────────────
# Per-user cooldown: maps chat_id → last /check_offer timestamp
_LAST_CHECK_TIME: dict[int, float] = {}
CHECK_OFFER_COOLDOWN = 5 * 60  # 5 minutes between checks per user

# Limit the number of simultaneous Chrome instances (1 for ≤4GB RAM servers)
_CHROME_SEMAPHORE = asyncio.Semaphore(1)

# ── Session storage ───────────────────────────────────────────────────────────
# In-memory dict keyed by Telegram chat_id.
# Values: {"email": bytearray, "password": bytearray, "device": DeviceProfile,
#          "offer_link": str|None, "created_at": float}
SESSION_STORE: dict[int, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_session(chat_id: int) -> dict:
    """Return (creating if absent) the session dict for *chat_id*.

    Automatically purges the session if it has exceeded the TTL.
    """
    session = SESSION_STORE.get(chat_id)
    if session and _is_session_expired(session):
        logger.info("Session expired for chat %s – purging", chat_id)
        _clear_session(chat_id)
        session = None
    if session is None:
        SESSION_STORE[chat_id] = {}
    return SESSION_STORE[chat_id]


def _is_session_expired(session: dict) -> bool:
    """Return True if *session* has exceeded the configured TTL."""
    created = session.get("created_at")
    if created is None:
        return False
    return (time.time() - created) > config.SESSION_TTL_SECONDS


def _secure_wipe(data: bytearray) -> None:
    """Zero-fill a bytearray in-place so the original bytes are unrecoverable."""
    for i in range(len(data)):
        data[i] = 0


def _clear_session(chat_id: int) -> None:
    """Securely wipe credentials and remove the session for *chat_id*."""
    session = SESSION_STORE.pop(chat_id, None)
    if session is None:
        return
    # Securely overwrite bytearray credentials in-place
    for key in ("password", "email"):
        val = session.get(key)
        if isinstance(val, bytearray):
            _secure_wipe(val)
    session.clear()
    logger.debug("Session cleared for chat %s", chat_id)


def _purge_expired_sessions() -> int:
    """Remove all expired sessions.  Returns the number purged."""
    expired = [
        cid for cid, sess in SESSION_STORE.items()
        if _is_session_expired(sess)
    ]
    for cid in expired:
        _clear_session(cid)
    if expired:
        logger.info("Purged %d expired session(s)", len(expired))
    return len(expired)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message with command menu."""
    await update.message.reply_text(
        "🤖 *Pixel 10 Pro Google One Bot*\n\n"
        "Bot này giả lập các thiết bị Google Pixel Pro (Pixel 9/10 Pro, XL, Fold – Android 16), "
        "đăng nhập vào tài khoản Google của bạn và lấy liên kết ưu đãi "
        "*Gemini Pro miễn phí 12 tháng* từ Google One.\n\n"
        "📋 *Các lệnh khả dụng:*\n"
        "• /login – Nhập thông tin đăng nhập tài khoản Google\n"
        "• /logout – Xóa thông tin đăng nhập đã lưu\n"
        "• /check\\_offer – Dò tìm ưu đãi Gemini Pro\n"
        "• /get\\_link – Hiển thị liên kết ưu đãi lấy được gần nhất\n"
        "• /status – Xem thông tin phiên \u0026 thiết bị hiện tại\n\n"
        "💡 *Mẹo:* Hỗ trợ cả tài khoản Gmail và Google Workspace.\n\n"
        "⚠️ *Lưu ý bảo mật:* Thông tin đăng nhập chỉ được giữ trong bộ nhớ "
        "trong suốt phiên làm việc và không bao giờ được lưu trữ lâu dài.",
        parse_mode="Markdown",
    )


# ── /login conversation ───────────────────────────────────────────────────────

async def login_start(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin the login conversation – ask for email."""
    await update.message.reply_text(
        "📧 Vui lòng nhập email tài khoản Google của bạn "
        "(Gmail hoặc Google Workspace):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AWAIT_EMAIL


async def login_email(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the email and ask for password."""
    email = update.message.text.strip()

    # Basic email format validation (Gmail and Google Workspace accounts)
    if not re.match(r'^[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}$', email, re.IGNORECASE):
        await update.message.reply_text(
            "⚠️ Vui lòng nhập một địa chỉ email hợp lệ "
            "(ví dụ: user@gmail.com hoặc user@company.com)."
        )
        return AWAIT_EMAIL

    # Optional domain restriction (empty list = accept any domain)
    allowed = config.ALLOWED_EMAIL_DOMAINS
    if allowed:
        domain = email.rsplit("@", 1)[1].lower()
        if domain not in [d.lower() for d in allowed]:
            domains_str = ", ".join(f"@{d}" for d in allowed)
            await update.message.reply_text(
                f"⚠️ Chỉ chấp nhận các tên miền email sau: "
                f"{domains_str}\n\nVui lòng thử lại."
            )
            return AWAIT_EMAIL

    context.user_data["pending_email"] = email
    await update.message.reply_text(
        f"✅ Đã nhận email: `{email}`\n\n🔒 Bây giờ hãy nhập mật khẩu của bạn:",
        parse_mode="Markdown",
    )
    return AWAIT_PASSWORD


async def login_password(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store credentials, generate a new device profile, and finish."""
    chat_id = update.effective_chat.id
    raw_input = update.message.text.strip()
    email = context.user_data.pop("pending_email", "")

    # Parse password|totp_secret format
    if "|" in raw_input:
        password, totp_secret = raw_input.split("|", 1)
        password = password.strip()
        totp_secret = totp_secret.strip()
    else:
        password = raw_input
        totp_secret = None

    session = _get_session(chat_id)
    # Store credentials as bytearray for secure in-place wiping
    session["email"] = bytearray(email.encode("utf-8"))
    session["password"] = bytearray(password.encode("utf-8"))
    if totp_secret:
        session["totp_secret"] = totp_secret
    session["device"] = create_device_profile()
    session["offer_link"] = None
    session["created_at"] = time.time()

    # Delete the message containing the password for security
    try:
        await update.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ *Đã lưu thông tin đăng nhập* và một hồ sơ thiết bị Pixel "
            "mới đã được tạo cho phiên này.\n\n"
            + session["device"].summary()
            + ("\U0001f511 Đã lưu khóa bí mật TOTP \u2013 2FA sẽ được xử lý tự động.\n\n"
               if totp_secret else "")
            + "Dùng /check\\_offer để tìm ưu đãi Gemini Pro."
        ),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def login_cancel(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the login conversation."""
    context.user_data.pop("pending_email", None)
    await update.message.reply_text(
        "❌ Đã hủy đăng nhập.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── /logout ───────────────────────────────────────────────────────────────────

async def logout(update: Update,
                 context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear stored credentials and destroy the session."""
    chat_id = update.effective_chat.id
    if chat_id in SESSION_STORE:
        _clear_session(chat_id)
        await update.message.reply_text(
            "🔒 Thông tin đăng nhập và dữ liệu phiên đã được xóa an toàn."
        )
    else:
        await update.message.reply_text(
            "ℹ️ Không có phiên nào đang hoạt động để xóa."
        )


# ── /check_offer ──────────────────────────────────────────────────────────────

async def _report_offer(update_or_chat_id, context, session, offer_link) -> None:
    """Send the offer result message."""
    chat_id = (update_or_chat_id if isinstance(update_or_chat_id, int)
               else update_or_chat_id.effective_chat.id)
    if offer_link:
        session["offer_link"] = offer_link
        text = (
            "🎉 <b>Đã tìm thấy ưu đãi Gemini Pro!</b>\n\n"
            "Nhấp vào liên kết bên dưới để kích hoạt Gemini Pro miễn phí 12 tháng:\n\n"
            f"🔗 {offer_link}\n\n"
            "Dùng /get_link để lấy lại liên kết này."
        )
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=text, parse_mode="HTML",
            )
        except Exception:
            # Fallback: send without formatting
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🎉 Đã tìm thấy ưu đãi Gemini Pro!\n\n🔗 {offer_link}\n\nDùng /get_link để lấy lại liên kết này.",
            )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "😔 Hiện chưa phát hiện ưu đãi Gemini Pro nào đang hoạt động "
                "trên tài khoản Google One của bạn.\n\n"
                "Ưu đãi có thể không khả dụng cho khu vực tài khoản của bạn hoặc "
                "đã được kích hoạt trước đó. Vui lòng thử lại sau."
            ),
        )


async def check_offer(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """Run Google One automation and report the result.

    If the offer is not found, retry with a new device profile up to
    ``_MAX_OFFER_ATTEMPTS`` times before reporting failure.
    """
    _MAX_OFFER_ATTEMPTS = 3
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if not session.get("email") or not session.get("password"):
        await update.message.reply_text(
            "⚠️ Không tìm thấy thông tin đăng nhập. Vui lòng dùng /login trước."
        )
        return ConversationHandler.END

    # ── Rate limit check ──────────────────────────────────────────────────
    last_check = _LAST_CHECK_TIME.get(chat_id, 0)
    elapsed = time.time() - last_check
    if elapsed < CHECK_OFFER_COOLDOWN:
        remaining = int(CHECK_OFFER_COOLDOWN - elapsed)
        mins, secs = divmod(remaining, 60)
        await update.message.reply_text(
            f"⏳ Vui lòng đợi {mins} phút {secs} giây trước khi kiểm tra lại."
        )
        return ConversationHandler.END
    _LAST_CHECK_TIME[chat_id] = time.time()

    # ── Concurrency check ─────────────────────────────────────────────────
    if _CHROME_SEMAPHORE.locked():
        await update.message.reply_text(
            "🔄 Hệ thống hiện đang hoạt động ở công suất tối đa. "
            "Vui lòng thử lại sau một phút."
        )
        _LAST_CHECK_TIME.pop(chat_id, None)
        return ConversationHandler.END

    await update.message.reply_text(
        "⏳ Đang khởi động trình giả lập thiết bị Pixel và đăng nhập…\n"
        "Quá trình này có thể mất tới 60 giây."
    )

    try:
        async with _CHROME_SEMAPHORE:
            # Decode bytearray credentials to str for Selenium
            email_str = bytes(session["email"]).decode("utf-8")
            pw_str = bytes(session["password"]).decode("utf-8")
            offer_link = None

            # Shuffle the presets so each attempt simulates a distinct Pixel model.
            rotation = random.sample(ALL_DEVICE_PROFILES, k=len(ALL_DEVICE_PROFILES))

            for attempt in range(1, _MAX_OFFER_ATTEMPTS + 1):
                # Rotate to a different Pixel preset on each attempt
                profile_name = rotation[(attempt - 1) % len(rotation)]
                device = create_device_profile(profile_name)
                session["device"] = device

                if attempt > 1:
                    await update.message.reply_text(
                        f"🔄 Lần thử {attempt}/{_MAX_OFFER_ATTEMPTS}: "
                        f"Đang giả lập {device.model} và thử lại…"
                    )

                # Start login in a thread
                driver = None
                try:
                    driver, status = await asyncio.to_thread(
                        start_login, email_str, pw_str, device,
                    )

                    if status == "needs_totp":
                        totp_secret = session.get("totp_secret")
                        if totp_secret:
                            try:
                                import pyotp
                                totp = pyotp.TOTP(totp_secret)
                                code = totp.now()
                                logger.info(
                                    "Auto-generated TOTP code for chat %s (attempt %d)",
                                    chat_id, attempt,
                                )

                                accepted = await asyncio.to_thread(
                                    submit_2fa_code, driver, code,
                                )
                                if accepted == TOTP_LOCKED:
                                    close_driver(driver)
                                    driver = None
                                    await update.message.reply_text(
                                        "⛔ Google đã tạm khóa xác minh 2FA cho "
                                        "tài khoản này do quá nhiều lần thử sai.\n"
                                        "Vui lòng đợi vài giờ rồi thử lại — đây "
                                        "không phải lỗi nhập sai mã."
                                    )
                                    return ConversationHandler.END
                                if not accepted:
                                    close_driver(driver)
                                    driver = None
                                    await update.message.reply_text(
                                        "❌ Mã TOTP tự tạo đã bị từ chối. "
                                        "Vui lòng kiểm tra lại khóa bí mật TOTP của bạn."
                                    )
                                    return ConversationHandler.END

                                # 2FA passed – notify and check offer
                                await update.message.reply_text(
                                    f"✅ Đăng nhập thành công (lần {attempt}/{_MAX_OFFER_ATTEMPTS}), "
                                    "đang kiểm tra ưu đãi Gemini Pro…"
                                )
                                offer_link = await asyncio.to_thread(
                                    check_offer_with_driver, driver,
                                )
                            except Exception as exc:
                                logger.warning("Auto-TOTP failed: %s", exc)
                                close_driver(driver)
                                driver = None
                                await update.message.reply_text(
                                    f"❌ Lỗi tự động nhập TOTP: {exc}\n"
                                    "Vui lòng kiểm tra lại khóa bí mật TOTP của bạn."
                                )
                                return ConversationHandler.END
                        else:
                            # No TOTP secret – ask user for code interactively
                            # (no retry for interactive 2FA)
                            session["_driver"] = driver
                            session["_2fa_attempts"] = 0
                            # IMPORTANT: clear local ref so the `finally` below does
                            # NOT close the driver we just handed off to the session.
                            driver = None
                            await update.message.reply_text(
                                "🔐 *Yêu cầu xác thực hai yếu tố (2FA)*\n\n"
                                "Vui lòng nhập mã 6 chữ số từ ứng dụng xác thực của bạn:",
                                parse_mode="Markdown",
                            )
                            return AWAIT_2FA_CODE
                    else:
                        # Login succeeded (no 2FA) – notify and check offer
                        await update.message.reply_text(
                            f"✅ Đăng nhập thành công (lần {attempt}/{_MAX_OFFER_ATTEMPTS}), "
                            "đang kiểm tra ưu đãi Gemini Pro…"
                        )
                        offer_link = await asyncio.to_thread(
                            check_offer_with_driver, driver,
                        )
                finally:
                    if driver:
                        close_driver(driver)

                # If offer found, stop retrying
                if offer_link:
                    logger.info(
                        "Offer found on attempt %d for chat %s: %s",
                        attempt, chat_id, offer_link,
                    )
                    break

                # Offer not found – log and wait before retrying
                logger.info(
                    "No offer found on attempt %d/%d for chat %s",
                    attempt, _MAX_OFFER_ATTEMPTS, chat_id,
                )

                # Wait before next attempt to avoid rate-limiting
                if attempt < _MAX_OFFER_ATTEMPTS:
                    delay = random.randint(15, 30)
                    await update.message.reply_text(
                        f"⏳ Chưa phát hiện ưu đãi, sẽ bắt đầu lần thử thứ {attempt + 1} sau {delay} giây…"
                    )
                    await asyncio.sleep(delay)
                    await update.message.reply_text(
                        f"🔄 Bắt đầu lần thử {attempt + 1}/{_MAX_OFFER_ATTEMPTS}, "
                        "đang tạo thiết bị mới và đăng nhập…"
                    )

    except GoogleAutomationError as exc:
        error_code = getattr(exc, "code", "automation_error")
        # Allow immediate retry for runtime/challenge failures.
        if error_code in {
            "login_timeout",
            "webdriver_crashed",
            "google_challenge",
            "login_unknown",
        }:
            _LAST_CHECK_TIME.pop(chat_id, None)
        await update.message.reply_text(
            f"❌ <b>Lỗi ({error_code}):</b> {exc}",
            parse_mode="HTML",
        )
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("Unexpected error in check_offer for chat %s", chat_id)
        await update.message.reply_text(
            f"❌ Đã xảy ra lỗi không mong muốn: {exc}"
        )
        return ConversationHandler.END
    finally:
        # Securely wipe password after use
        pw = session.get("password")
        if isinstance(pw, bytearray):
            _secure_wipe(pw)
        session.pop("password", None)

    if not offer_link:
        await update.message.reply_text(
            f"❌ Sau {_MAX_OFFER_ATTEMPTS} lần thử, không tìm thấy ưu đãi Gemini Pro.\n\n"
            "Tài khoản của bạn không đủ điều kiện nhận Gemini Pro miễn phí 12 tháng trên thiết bị Pixel.\n"
            "Các nguyên nhân có thể:\n"
            "• Khu vực tài khoản không được hỗ trợ\n"
            "• Đã có gói đăng ký Gemini Pro đang hoạt động\n"
            "• Tài khoản thuộc nhóm gia đình và đã có thành viên đăng ký\n"
            "• Tài khoản mới đăng ký bị kiểm soát rủi ro"
        )
        return ConversationHandler.END

    await _report_offer(update, context, session, offer_link)
    return ConversationHandler.END


async def handle_2fa_code(update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the TOTP code submitted by the user during 2FA."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)
    code = update.message.text.strip()

    # Delete the message containing the code for security
    try:
        await update.message.delete()
    except Exception:
        pass

    driver = session.pop("_driver", None)
    if not driver:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Phiên đã hết hạn. Vui lòng chạy /check_offer lại.",
        )
        return ConversationHandler.END
    attempts = int(session.get("_2fa_attempts", 0))

    # Validate code format
    if not code.isdigit() or len(code) != 6:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Mã không hợp lệ. Vui lòng nhập một số gồm 6 chữ số.",
        )
        session["_driver"] = driver  # Put driver back
        return AWAIT_2FA_CODE

    await context.bot.send_message(
        chat_id=chat_id,
        text="🔄 Đang xác minh mã…",
    )

    try:
        async with _CHROME_SEMAPHORE:
            accepted = await asyncio.to_thread(
                submit_2fa_code, driver, code,
            )

            if accepted is None:
                close_driver(driver)
                session.pop("_2fa_attempts", None)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ Phiên trình duyệt đã gặp sự cố khi đang xác minh mã 2FA.\n"
                        "Đây không phải do nhập sai mã. Vui lòng chạy /check\\_offer lại."
                    ),
                )
                return ConversationHandler.END

            if accepted == TOTP_LOCKED:
                close_driver(driver)
                session.pop("_2fa_attempts", None)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "⛔ Google đã tạm khóa xác minh 2FA cho tài khoản này "
                        "do quá nhiều lần thử sai.\n"
                        "Vui lòng đợi vài giờ rồi thử lại. Đây không phải do "
                        "bạn nhập sai mã."
                    ),
                )
                return ConversationHandler.END

            if not accepted:
                attempts += 1
                session["_2fa_attempts"] = attempts
                if attempts < MAX_2FA_ATTEMPTS:
                    session["_driver"] = driver
                    remaining = MAX_2FA_ATTEMPTS - attempts
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"❌ Mã bị từ chối. Vui lòng nhập mã 6 chữ số mới.\n"
                            f"Số lần thử còn lại: {remaining}"
                        ),
                    )
                    return AWAIT_2FA_CODE
                close_driver(driver)
                session.pop("_2fa_attempts", None)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ Mã bị từ chối quá nhiều lần. "
                        "Vui lòng chạy /check\\_offer lại."
                    ),
                )
                return ConversationHandler.END

            # 2FA passed – check offer
            try:
                offer_link = await asyncio.to_thread(
                    check_offer_with_driver, driver,
                )
            finally:
                close_driver(driver)
                session.pop("_2fa_attempts", None)

    except Exception as exc:
        logger.exception("Error in 2FA for chat %s", chat_id)
        close_driver(driver)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Lỗi: {exc}",
        )
        return ConversationHandler.END
    finally:
        pw = session.get("password")
        if isinstance(pw, bytearray):
            _secure_wipe(pw)
        session.pop("password", None)

    await _report_offer(chat_id, context, session, offer_link)
    return ConversationHandler.END


async def cancel_2fa(update: Update,
                     context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel 2FA input and close the driver."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)
    driver = session.pop("_driver", None)
    session.pop("_2fa_attempts", None)
    close_driver(driver)
    await update.message.reply_text("❌ Đã hủy 2FA.")
    return ConversationHandler.END


# ── /get_link ─────────────────────────────────────────────────────────────────

async def get_link(update: Update,
                   context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the last captured offer link for this session."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)
    link = session.get("offer_link")

    if link:
        await update.message.reply_text(
            f"🔗 <b>Liên kết ưu đãi lấy được gần nhất:</b>\n\n{link}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "ℹ️ Chưa lấy được liên kết ưu đãi nào. "
            "Dùng /check\\_offer để tìm ưu đãi Gemini Pro.",
            parse_mode="Markdown",
        )


# ── /status ───────────────────────────────────────────────────────────────────

async def status(update: Update,
                 context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current session and device profile summary."""
    chat_id = update.effective_chat.id

    if chat_id not in SESSION_STORE or not SESSION_STORE[chat_id]:
        await update.message.reply_text(
            "ℹ️ Không có phiên nào đang hoạt động. Dùng /login để bắt đầu."
        )
        return

    session = SESSION_STORE[chat_id]

    email_raw = session.get("email", "—")
    # Decode bytearray email for display
    if isinstance(email_raw, bytearray):
        email = bytes(email_raw).decode("utf-8")
    else:
        email = str(email_raw) if email_raw else "—"
    has_creds = bool(session.get("email") and session.get("password"))
    offer_link = session.get("offer_link")
    device = session.get("device")

    lines = [
        "📊 *Trạng thái phiên*\n",
        f"Tài khoản: `{email}`",
        f"Đã nạp thông tin đăng nhập: {'✅' if has_creds else '❌'}",
        f"Đã lấy liên kết ưu đãi: {'✅' if offer_link else '❌'}",
    ]

    if device:
        lines.append("\n" + device.summary())

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )



# ── Periodic cleanup ──────────────────────────────────────────────────────────

async def _session_cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic callback to purge expired sessions and orphaned Chrome processes."""
    _purge_expired_sessions()
    # Sweep any orphaned Chromium processes not cleaned by close_driver
    # (only runs when semaphore is free, i.e. no active session)
    if not _CHROME_SEMAPHORE.locked():
        try:
            import subprocess
            result = subprocess.run(
                ["pkill", "-9", "-f", "chromium"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                logger.info("Periodic cleanup: killed orphaned Chromium process(es)")
        except Exception:
            pass


# ── Application setup ─────────────────────────────────────────────────────────

def main() -> None:
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN environment variable is not set. "
            "Set it as an environment variable (e.g. via .env file or "
            "system environment) and restart."
        )
        sys.exit(1)

    app = Application.builder().token(token).build()

    # /login conversation
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            AWAIT_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_email)
            ],
            AWAIT_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)
            ],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
        allow_reentry=True,
    )

    # /check_offer conversation (handles 2FA)
    async def _offer_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle conversation timeout – clean up driver."""
        if update and update.effective_chat:
            chat_id = update.effective_chat.id
            session = SESSION_STORE.get(chat_id, {})
            driver = session.pop("_driver", None)
            session.pop("_2fa_attempts", None)
            close_driver(driver)
            await context.bot.send_message(
                chat_id=chat_id,
                text="⏰ Xác minh 2FA đã hết thời gian chờ. Vui lòng chạy /check_offer lại.",
            )
        return ConversationHandler.END

    offer_conv = ConversationHandler(
        entry_points=[CommandHandler("check_offer", check_offer)],
        states={
            AWAIT_2FA_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_2fa_code)
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, _offer_timeout)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_2fa)],
        allow_reentry=True,
        conversation_timeout=120,  # 2 minutes to enter 2FA code
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(login_conv)
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(offer_conv)
    app.add_handler(CommandHandler("get_link", get_link))
    app.add_handler(CommandHandler("status", status))

    # Periodic job: purge expired sessions every 5 minutes
    app.job_queue.run_repeating(
        _session_cleanup_job, interval=300, first=300,
    )

    logger.info("Bot is running. Press Ctrl-C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
