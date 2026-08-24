"""In-memory staff authentication and bounded completed-round search."""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from app.domain.models import RoundRecord
from app.persistence.rounds import ShelfDbRoundRepository

LOGGER = logging.getLogger("photo_prompt.staff")
STAFF_PIN_ENV = "PHOTO_PROMPT_STAFF_PIN"
SESSION_COOKIE = "photo-prompt-staff"
LOGIN_CSRF_COOKIE = "photo-prompt-staff-login-csrf"
SESSION_MAX_AGE = 12 * 60 * 60
SESSION_PATH = "/staff"
SEARCH_PAGE_SIZE = 4
_COOLDOWN = timedelta(seconds=30)
_SESSION_LIFETIME = timedelta(hours=12)
_LOGIN_CSRF_LIFETIME = timedelta(minutes=15)
_BANGKOK = ZoneInfo("Asia/Bangkok")
_UUID_RE = re.compile(
    r"^/generated/([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.png$"
)


@dataclass
class StaffSession:
    token: str
    csrf_token: str
    created_at: datetime
    last_seen: datetime
    search_term: str = ""
    page: int = 1


@dataclass(frozen=True)
class StaffRoundResult:
    """Private result projection; it deliberately contains no prompt text."""

    round_id: str
    display_name: str
    level: str
    score: int | float
    completed_at: str
    formatted_completed_at: str
    image_url: str | None
    image_available: bool


@dataclass(frozen=True)
class StaffSearchPage:
    rows: tuple[StaffRoundResult, ...]
    page: int
    page_count: int
    total: int
    term: str


class StaffAuth:
    """Authenticate staff with a secret-only PIN and opaque in-memory sessions."""

    def __init__(
        self,
        pin: str | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._pin = pin if pin is not None else os.environ.get(STAFF_PIN_ENV)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sessions: dict[str, StaffSession] = {}
        self._login_csrf: dict[str, datetime] = {}
        self._failures: dict[str, tuple[int, datetime | None]] = {}
        if not self.available:
            LOGGER.warning("staff search disabled because the staff PIN is missing or invalid")

    @property
    def available(self) -> bool:
        return isinstance(self._pin, str) and bool(re.fullmatch(r"[0-9]{6}", self._pin))

    def issue_login_csrf(self) -> str | None:
        if not self.available:
            return None
        self._clean()
        token = secrets.token_urlsafe(24)
        self._login_csrf[token] = self._now() + _LOGIN_CSRF_LIFETIME
        return token

    def verify_login(
        self,
        pin: str,
        csrf_token: str,
        csrf_cookie: str | None,
        client_key: str,
    ) -> str:
        if not self.available:
            raise StaffUnavailableError
        self._clean()
        now = self._now()
        if not self._verify_login_csrf(csrf_token, csrf_cookie, now):
            raise StaffCSRFError
        failures, cooldown_until = self._failures.get(client_key, (0, None))
        if cooldown_until is not None and cooldown_until > now:
            raise StaffCooldownError
        if not isinstance(pin, str) or not hmac.compare_digest(pin, self._pin or ""):
            failures += 1
            if failures >= 5:
                self._failures[client_key] = (0, now + _COOLDOWN)
            else:
                self._failures[client_key] = (failures, None)
            raise StaffLoginError
        self._failures.pop(client_key, None)
        token = secrets.token_urlsafe(32)
        self._sessions[token] = StaffSession(
            token=token,
            csrf_token=secrets.token_urlsafe(24),
            created_at=now,
            last_seen=now,
        )
        self._login_csrf.pop(csrf_cookie or "", None)
        return token

    def session(self, token: str | None) -> StaffSession | None:
        if not self.available or not token:
            return None
        self._clean()
        session = self._sessions.get(token)
        if session is None:
            return None
        if self._now() - session.created_at >= _SESSION_LIFETIME:
            self._sessions.pop(token, None)
            return None
        session.last_seen = self._now()
        return session

    def verify_csrf(self, session: StaffSession, submitted: str | None) -> bool:
        return isinstance(submitted, str) and hmac.compare_digest(session.csrf_token, submitted)

    def logout(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def _verify_login_csrf(self, submitted: str, cookie: str | None, now: datetime) -> bool:
        if not isinstance(cookie, str) or not isinstance(submitted, str):
            return False
        expiry = self._login_csrf.get(cookie)
        return expiry is not None and expiry > now and hmac.compare_digest(cookie, submitted)

    def _clean(self) -> None:
        now = self._now()
        self._sessions = {
            token: session
            for token, session in self._sessions.items()
            if now - session.created_at < _SESSION_LIFETIME
        }
        self._login_csrf = {
            token: expiry for token, expiry in self._login_csrf.items() if expiry > now
        }
        self._failures = {
            key: value
            for key, value in self._failures.items()
            if value[1] is None or value[1] > now or value[0] > 0
        }

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("staff clock must be timezone-aware")
        return current.astimezone(UTC)


class StaffUnavailableError(RuntimeError):
    pass


class StaffLoginError(ValueError):
    pass


class StaffCooldownError(ValueError):
    pass


class StaffCSRFError(ValueError):
    pass


def search_completed_rounds(
    repository: ShelfDbRoundRepository,
    generated_root: Path | None,
    *,
    term: str,
    page: int,
) -> StaffSearchPage:
    """Search at most the event-sized completed-round collection in memory."""

    normalized = term.strip()
    folded = normalized.casefold()
    rounds = repository.list_completed()
    matching = [
        record
        for record in rounds
        if not folded or folded in record.display_name.strip().casefold()
    ]
    matching.sort(key=lambda record: (record.completed_at or "", record.id), reverse=True)
    safe_page = max(1, page)
    page_count = max(1, (len(matching) + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)
    safe_page = min(safe_page, page_count)
    start = (safe_page - 1) * SEARCH_PAGE_SIZE
    rows = tuple(
        _round_result(record, generated_root)
        for record in matching[start : start + SEARCH_PAGE_SIZE]
    )
    return StaffSearchPage(rows, safe_page, page_count, len(matching), normalized)


def _round_result(record: RoundRecord, generated_root: Path | None) -> StaffRoundResult:
    artifact = record.generated_artifact
    image_available = bool(
        artifact and generated_root and artifact_is_available(artifact.url, generated_root)
    )
    return StaffRoundResult(
        round_id=record.id,
        display_name=record.display_name,
        level=record.level.value if record.level is not None else "",
        score=record.score.total_score if record.score is not None else 0,
        completed_at=record.completed_at or record.updated_at,
        formatted_completed_at=_format_bangkok(record.completed_at or record.updated_at),
        image_url=artifact.url if artifact and image_available else None,
        image_available=image_available,
    )


def artifact_is_available(url: str, generated_root: Path) -> bool:
    parsed = urlsplit(url)
    match = _UUID_RE.fullmatch(parsed.path)
    if match is None or parsed.query or parsed.fragment or parsed.scheme or parsed.netloc:
        return False
    try:
        UUID(match.group(1))
        UUID(match.group(2))
    except ValueError:
        return False
    root = generated_root.resolve(strict=False)
    candidate = (root / match.group(1) / f"{match.group(2)}.png").resolve(strict=False)
    try:
        candidate.relative_to(root)
        return candidate.is_file() and not candidate.is_symlink()
    except ValueError:
        return False


def _format_bangkok(timestamp: str) -> str:
    candidate = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    return datetime.fromisoformat(candidate).astimezone(_BANGKOK).strftime("%d/%m/%Y %H:%M")


__all__ = [
    "LOGIN_CSRF_COOKIE",
    "SESSION_COOKIE",
    "SESSION_MAX_AGE",
    "SESSION_PATH",
    "SEARCH_PAGE_SIZE",
    "StaffAuth",
    "StaffCSRFError",
    "StaffCooldownError",
    "StaffLoginError",
    "StaffRoundResult",
    "StaffSearchPage",
    "StaffSession",
    "StaffUnavailableError",
    "artifact_is_available",
    "search_completed_rounds",
]
