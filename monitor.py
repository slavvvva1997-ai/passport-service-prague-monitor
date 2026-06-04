# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from requests.exceptions import RequestException


DEFAULT_URL = "https://prague.pasport.org.ua/solutions/e-queue"
DEFAULT_COOLDOWN_SECONDS = 1800
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; PassportServicePragueMonitor/1.0; "
    "+https://prague.pasport.org.ua/solutions/e-queue)"
)
DEFAULT_BROWSER_WAIT_SECONDS = 5
STATE_KEY = "passport_service_prague"
STATE_FILE_DEFAULT = "state.json"
TEST_TELEGRAM_MESSAGE = "✅ Онлайн-мониторинг Паспортного сервиса запущен."

UNAVAILABLE_PHRASES = (
    "Сервіс не доступний",
    "сервіс не доступний",
    "не доступний",
    "обмежувати доступ",
    "VPN",
)

POSSIBLY_AVAILABLE_PHRASES = (
    "вільні місця",
    "оберіть дату",
    "виберіть дату",
    "записатися",
    "реєстрація",
    "електронна черга",
)

BLOCKED_PAGE_PHRASES = (
    "Just a moment",
    "Enable JavaScript and cookies to continue",
    "Checking your browser",
    "Cloudflare",
    "captcha",
)

logger = logging.getLogger("passport_monitor")


@dataclass(frozen=True)
class Config:
    url: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    notify_cooldown_seconds: int
    request_timeout_seconds: int
    browser_wait_seconds: int
    user_agent: str
    database_url: str | None
    state_backend: str | None
    github_repository: str | None
    github_token: str | None
    github_state_variable: str
    github_state_path: str
    state_file: Path


@dataclass(frozen=True)
class MonitorResult:
    url: str
    http_status_code: int | None
    status: str
    text: str
    text_hash: str | None
    error_message: str | None


@dataclass(frozen=True)
class NotificationDecision:
    should_send: bool
    notification_key: str | None
    message: str | None
    reason: str


class StateStore(Protocol):
    def load(self) -> dict[str, Any]:
        ...

    def save(self, state: dict[str, Any]) -> None:
        ...


class LocalJsonStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}

        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read local state file: %s", exc)
            return {}

    def save(self, state: dict[str, Any]) -> None:
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )


class SQLiteStateStore:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = str(database_path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

    def _ensure_table(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS monitor_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.commit()

    def load(self) -> dict[str, Any]:
        with self._connect() as connection:
            self._ensure_table(connection)
            row = connection.execute(
                "SELECT value FROM monitor_state WHERE key = ?",
                (STATE_KEY,),
            ).fetchone()

        if row is None:
            return {}

        try:
            return json.loads(row[0])
        except json.JSONDecodeError as exc:
            logger.warning("Could not parse SQLite state: %s", exc)
            return {}

    def save(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        now = utc_now_iso()

        with self._connect() as connection:
            self._ensure_table(connection)
            connection.execute(
                """
                INSERT INTO monitor_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (STATE_KEY, payload, now),
            )
            connection.commit()


class PostgresStateStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = normalize_postgres_url(database_url)

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "DATABASE_URL points to PostgreSQL, but psycopg is not installed."
            ) from exc

        return psycopg.connect(self.database_url, autocommit=True)

    def _ensure_table(self, connection: Any) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS monitor_state (
                key TEXT PRIMARY KEY,
                value JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

    def load(self) -> dict[str, Any]:
        with self._connect() as connection:
            self._ensure_table(connection)
            row = connection.execute(
                "SELECT value FROM monitor_state WHERE key = %s",
                (STATE_KEY,),
            ).fetchone()

        if row is None:
            return {}

        value = row[0]
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            return json.loads(value)
        return {}

    def save(self, state: dict[str, Any]) -> None:
        try:
            from psycopg.types.json import Json
        except ImportError as exc:
            raise RuntimeError("Could not import psycopg JSON adapter.") from exc

        with self._connect() as connection:
            self._ensure_table(connection)
            connection.execute(
                """
                INSERT INTO monitor_state (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = NOW()
                """,
                (STATE_KEY, Json(state)),
            )


class GitHubActionsVariableStateStore:
    def __init__(self, repository: str, token: str, variable_name: str) -> None:
        self.repository = repository
        self.token = token
        self.variable_name = variable_name
        self.api_url = (
            f"https://api.github.com/repos/{repository}/actions/variables"
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def load(self) -> dict[str, Any]:
        response = requests.get(
            f"{self.api_url}/{self.variable_name}",
            headers=self._headers(),
            timeout=20,
        )
        if response.status_code == 404:
            return {}

        response.raise_for_status()
        raw_value = response.json().get("value")
        if not raw_value:
            return {}

        try:
            return json.loads(raw_value)
        except json.JSONDecodeError as exc:
            logger.warning("Could not parse GitHub state variable: %s", exc)
            return {}

    def save(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        update_response = requests.patch(
            f"{self.api_url}/{self.variable_name}",
            headers=self._headers(),
            json={"name": self.variable_name, "value": payload},
            timeout=20,
        )
        if update_response.status_code != 404:
            update_response.raise_for_status()
            return

        create_response = requests.post(
            self.api_url,
            headers=self._headers(),
            json={"name": self.variable_name, "value": payload},
            timeout=20,
        )
        create_response.raise_for_status()


class GitHubContentsStateStore:
    def __init__(self, repository: str, token: str, path: str) -> None:
        self.repository = repository
        self.token = token
        self.path = path
        self.api_url = f"https://api.github.com/repos/{repository}/contents/{path}"

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _read_remote(self) -> tuple[dict[str, Any], str | None]:
        response = requests.get(self.api_url, headers=self._headers(), timeout=20)
        if response.status_code == 404:
            return {}, None

        response.raise_for_status()
        body = response.json()
        encoded_content = body.get("content", "")
        cleaned_content = encoded_content.replace("\n", "")
        decoded = base64.b64decode(cleaned_content).decode("utf-8")
        return json.loads(decoded), body.get("sha")

    def load(self) -> dict[str, Any]:
        try:
            state, _ = self._read_remote()
            return state
        except json.JSONDecodeError as exc:
            logger.warning("Could not parse GitHub contents state: %s", exc)
            return {}

    def save(self, state: dict[str, Any]) -> None:
        current_state, sha = self._read_remote()
        if state_without_check_time(current_state) == state_without_check_time(state):
            return

        payload = json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        body: dict[str, Any] = {
            "message": "chore: update monitor state",
            "content": base64.b64encode(payload.encode("utf-8")).decode("ascii"),
        }
        if sha:
            body["sha"] = sha

        response = requests.put(
            self.api_url,
            headers=self._headers(),
            json=body,
            timeout=20,
        )
        response.raise_for_status()


def setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_int_env(name: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError:
        logger.warning("Invalid %s value; using default %s", name, default)
        return default

    if value < minimum:
        logger.warning("%s is below %s; using default %s", name, minimum, default)
        return default

    return value


def load_config() -> Config:
    load_dotenv()

    return Config(
        url=os.getenv("URL", DEFAULT_URL),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        notify_cooldown_seconds=read_int_env(
            "NOTIFY_COOLDOWN_SECONDS",
            DEFAULT_COOLDOWN_SECONDS,
        ),
        request_timeout_seconds=read_int_env(
            "REQUEST_TIMEOUT_SECONDS",
            DEFAULT_TIMEOUT_SECONDS,
        ),
        browser_wait_seconds=read_int_env(
            "BROWSER_WAIT_SECONDS",
            DEFAULT_BROWSER_WAIT_SECONDS,
        ),
        user_agent=os.getenv("USER_AGENT", DEFAULT_USER_AGENT),
        database_url=os.getenv("DATABASE_URL"),
        state_backend=os.getenv("STATE_BACKEND"),
        github_repository=os.getenv("GITHUB_REPOSITORY"),
        github_token=os.getenv("GITHUB_TOKEN"),
        github_state_variable=os.getenv(
            "GITHUB_STATE_VARIABLE",
            "PASSPORT_SERVICE_PRAGUE_STATE",
        ),
        github_state_path=os.getenv(
            "GITHUB_STATE_PATH",
            ".monitor-state/state.json",
        ),
        state_file=Path(os.getenv("STATE_FILE", STATE_FILE_DEFAULT)),
    )


def normalize_postgres_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql://", 1)
    return database_url


def sqlite_path_from_url(database_url: str) -> Path | str:
    path = database_url.removeprefix("sqlite:///")
    if path == ":memory:":
        return path
    return Path(path)


def create_state_store(config: Config) -> StateStore:
    if config.state_backend == "github_contents":
        if config.github_repository and config.github_token:
            return GitHubContentsStateStore(
                repository=config.github_repository,
                token=config.github_token,
                path=config.github_state_path,
            )
        logger.warning("GitHub contents state backend requested but not configured.")

    if config.state_backend == "github_actions":
        if config.github_repository and config.github_token:
            return GitHubActionsVariableStateStore(
                repository=config.github_repository,
                token=config.github_token,
                variable_name=config.github_state_variable,
            )
        logger.warning("GitHub Actions state backend requested but not configured.")

    if not config.database_url:
        return LocalJsonStateStore(config.state_file)

    database_url = config.database_url
    lowered_url = database_url.casefold()

    if lowered_url.startswith("sqlite:///"):
        return SQLiteStateStore(sqlite_path_from_url(database_url))

    if lowered_url.startswith(("postgres://", "postgresql://")):
        return PostgresStateStore(database_url)

    logger.warning("Unsupported DATABASE_URL scheme; using local state file.")
    return LocalJsonStateStore(config.state_file)


def state_without_check_time(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in state.items()
        if key != "last_checked_at"
    }


def clean_html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def determine_status(text: str) -> str:
    lowered_text = text.casefold()
    blocked = any(phrase.casefold() in lowered_text for phrase in BLOCKED_PAGE_PHRASES)
    if blocked:
        return "blocked"

    unavailable = any(
        phrase.casefold() in lowered_text for phrase in UNAVAILABLE_PHRASES
    )
    if unavailable:
        return "unavailable"

    possibly_available = any(
        phrase.casefold() in lowered_text for phrase in POSSIBLY_AVAILABLE_PHRASES
    )
    if possibly_available:
        return "possibly_available"

    if text:
        return "unknown"

    return "error"


def status_from_http_error(status_code: int | None) -> str:
    if status_code in {403, 429}:
        return "blocked"
    return "error"


def fetch_page(config: Config) -> MonitorResult:
    timeout_ms = config.request_timeout_seconds * 1000

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
            context = browser.new_context(
                user_agent=config.user_agent,
                locale="uk-UA",
                timezone_id="Europe/Prague",
                viewport={"width": 1365, "height": 900},
                extra_http_headers={
                    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
                    "Cache-Control": "no-cache",
                },
            )
            page = context.new_page()

            try:
                response = page.goto(
                    config.url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                page.wait_for_timeout(config.browser_wait_seconds * 1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5_000)
                except PlaywrightTimeoutError:
                    pass

                http_status_code = response.status if response else None
                html = page.content()
            finally:
                context.close()
                browser.close()
    except PlaywrightTimeoutError as exc:
        return MonitorResult(
            url=config.url,
            http_status_code=None,
            status="blocked",
            text="",
            text_hash=None,
            error_message=f"Playwright timeout: {exc}",
        )
    except PlaywrightError as exc:
        return MonitorResult(
            url=config.url,
            http_status_code=None,
            status="error",
            text="",
            text_hash=None,
            error_message=f"Playwright failed: {exc}",
        )

    text = clean_html_text(html)
    text_hash = hash_text(text) if text else None

    if http_status_code is not None and http_status_code >= 400:
        return MonitorResult(
            url=config.url,
            http_status_code=http_status_code,
            status=status_from_http_error(http_status_code),
            text=text,
            text_hash=text_hash,
            error_message=f"HTTP {http_status_code}",
        )

    return MonitorResult(
        url=config.url,
        http_status_code=http_status_code,
        status=determine_status(text),
        text=text,
        text_hash=text_hash,
        error_message=None,
    )


def build_message_for_status(status: str, url: str, error_message: str | None) -> str:
    if status == "possibly_available":
        return (
            "🚨 Возможно появились места на подачу паспорта в Праге.\n\n"
            "Статус: possibly_available\n"
            f"Сайт: {url}\n\n"
            "Зайди вручную и попробуй записаться. "
            "Скрипт не записывает автоматически."
        )

    if status == "unavailable":
        return "ℹ️ Паспортний сервіс Прага: запись всё ещё недоступна."

    if status == "blocked":
        return (
            "⚠️ Мониторинг не смог проверить сайт. "
            "Возможна блокировка запроса сервером.\n\n"
            "Проверь сайт вручную:\n"
            f"{url}"
        )

    if status == "error":
        safe_error = (error_message or "unknown error")[:300]
        return (
            "⚠️ Мониторинг Паспортного сервиса завершился с ошибкой.\n\n"
            "Статус: error\n"
            f"Ошибка: {safe_error}\n\n"
            "Проверь сайт вручную:\n"
            f"{url}"
        )

    return (
        "ℹ️ Паспортный сервис Прага: статус страницы изменился на unknown.\n\n"
        "Проверь вручную:\n"
        f"{url}"
    )


def build_notification_decision(
    result: MonitorResult,
    previous_state: dict[str, Any],
    cooldown_seconds: int,
    now: datetime,
) -> NotificationDecision:
    previous_status = previous_state.get("status")
    previous_hash = previous_state.get("text_hash")
    previous_error = previous_state.get("last_error")

    first_run = previous_status is None and previous_hash is None
    status_changed = result.status != previous_status
    hash_changed = (
        previous_hash is not None
        and result.text_hash is not None
        and result.text_hash != previous_hash
    )
    error_changed = (
        result.status in {"blocked", "error"}
        and result.error_message is not None
        and result.error_message != previous_error
    )

    notification_key: str | None = None
    message: str | None = None
    reason = "no_change"

    if result.status in {"blocked", "error"}:
        if first_run or status_changed or error_changed:
            notification_key = (
                f"{result.status}:{result.http_status_code}:{result.error_message}"
            )
            message = build_message_for_status(
                result.status,
                result.url,
                result.error_message,
            )
            reason = "new_error_or_block"
    elif result.status in {"possibly_available", "unavailable", "unknown"}:
        if first_run or status_changed:
            notification_key = f"status:{result.status}"
            message = build_message_for_status(result.status, result.url, None)
            reason = "first_run_or_status_changed"
        elif hash_changed:
            notification_key = f"page_changed:{result.status}"
            message = (
                "ℹ️ Страница Паспортного сервиса изменилась. Проверь вручную."
            )
            reason = "hash_changed"

    if notification_key is None or message is None:
        return NotificationDecision(False, None, None, reason)

    last_notification_key = previous_state.get("last_notification_key")
    last_notification_at = parse_iso_datetime(
        previous_state.get("last_notification_at")
    )
    cooldown_active = (
        notification_key == last_notification_key
        and last_notification_at is not None
        and (now - last_notification_at).total_seconds() < cooldown_seconds
    )

    if cooldown_active:
        return NotificationDecision(False, notification_key, message, "cooldown")

    return NotificationDecision(True, notification_key, message, reason)


def send_telegram_message(config: Config, message: str) -> tuple[bool, str | None]:
    if not config.telegram_bot_token or not config.telegram_chat_id:
        return False, "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"

    api_url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": config.telegram_chat_id,
        "text": message,
        "disable_web_page_preview": "true",
    }

    try:
        response = requests.post(
            api_url,
            data=payload,
            timeout=config.request_timeout_seconds,
        )
    except RequestException as exc:
        return False, f"Telegram request failed: {exc}"

    if response.status_code >= 400:
        return False, f"Telegram HTTP {response.status_code}: {response.text[:300]}"

    try:
        body = response.json()
    except ValueError:
        return False, "Telegram returned non-JSON response"

    if body.get("ok") is not True:
        return False, f"Telegram API returned ok=false: {str(body)[:300]}"

    return True, None


def build_next_state(
    result: MonitorResult,
    previous_state: dict[str, Any],
    notification_sent: bool,
    notification_key: str | None,
    checked_at: str,
) -> dict[str, Any]:
    base_state = {
        "status": result.status,
        "text_hash": result.text_hash,
        "last_checked_at": checked_at,
        "last_notification_at": previous_state.get("last_notification_at"),
        "last_notification_key": previous_state.get("last_notification_key"),
        "last_error": result.error_message,
        "http_status_code": result.http_status_code,
    }

    if not notification_sent:
        return base_state

    return {
        **base_state,
        "last_notification_at": checked_at,
        "last_notification_key": notification_key,
    }


def log_run_summary(
    result: MonitorResult,
    notification_sent: bool,
    error_message: str | None,
    checked_at: str,
) -> None:
    event = {
        "timestamp": checked_at,
        "url": result.url,
        "http_status": result.http_status_code,
        "parsed_status": result.status,
        "text_hash": result.text_hash,
        "notification_sent": notification_sent,
        "error": error_message,
    }
    logger.info(json.dumps(event, ensure_ascii=False, sort_keys=True))


def print_dry_run(result: MonitorResult) -> None:
    print(f"STATUS: {result.status}")
    print(f"HTTP_STATUS: {result.http_status_code}")
    print(f"TEXT_HASH: {result.text_hash}")
    print(f"ERROR: {result.error_message or ''}")
    print("TEXT_PREVIEW:")
    print(result.text[:500])


def run_monitor(config: Config, dry_run: bool = False) -> int:
    checked_at = utc_now_iso()
    result = fetch_page(config)

    if dry_run:
        print_dry_run(result)
        log_run_summary(
            result=result,
            notification_sent=False,
            error_message=result.error_message,
            checked_at=checked_at,
        )
        return 0

    store = create_state_store(config)
    state_error: str | None = None

    try:
        previous_state = store.load()
    except Exception as exc:
        previous_state = {}
        state_error = f"Could not load state: {exc}"
        logger.error(state_error)

    now = parse_iso_datetime(checked_at) or utc_now()
    decision = build_notification_decision(
        result=result,
        previous_state=previous_state,
        cooldown_seconds=config.notify_cooldown_seconds,
        now=now,
    )

    notification_sent = False
    notification_error: str | None = None

    if decision.should_send and decision.message:
        notification_sent, notification_error = send_telegram_message(
            config,
            decision.message,
        )

    next_state = build_next_state(
        result=result,
        previous_state=previous_state,
        notification_sent=notification_sent,
        notification_key=decision.notification_key,
        checked_at=checked_at,
    )

    try:
        store.save(next_state)
    except Exception as exc:
        state_error = f"Could not save state: {exc}"
        logger.error(state_error)

    combined_error = result.error_message or notification_error or state_error
    log_run_summary(
        result=result,
        notification_sent=notification_sent,
        error_message=combined_error,
        checked_at=checked_at,
    )

    return 0


def run_test_telegram(config: Config) -> int:
    ok, error_message = send_telegram_message(config, TEST_TELEGRAM_MESSAGE)
    checked_at = utc_now_iso()
    event = {
        "timestamp": checked_at,
        "url": config.url,
        "http_status": None,
        "parsed_status": "test_telegram",
        "text_hash": None,
        "notification_sent": ok,
        "error": error_message,
    }
    logger.info(json.dumps(event, ensure_ascii=False, sort_keys=True))

    if error_message:
        print(error_message)

    return 0 if ok else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Passport Service e-queue status in Prague."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check the page and print parsed data without Telegram or state writes.",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test Telegram message and exit.",
    )
    return parser.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()
    config = load_config()

    try:
        if args.test_telegram:
            return run_test_telegram(config)

        return run_monitor(config, dry_run=args.dry_run)
    except Exception as exc:
        logger.exception("Unhandled monitor error: %s", exc)
        return 0


if __name__ == "__main__":
    sys.exit(main())
