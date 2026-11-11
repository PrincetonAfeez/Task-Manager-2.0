"""Natural-language quick-add parser.

Parses ``"Ship release notes tomorrow 5pm #ops @alice !3 weekly"`` into structured fields.

Design constraints:

* **Never lose the user's text.** If a token is not recognised it stays in the title;
  silently dropping words is worse than not parsing them.
* **Deterministic given ``now``.** Every relative date resolves against an injected clock so
  the behaviour is testable and does not shift under the caller.
* **Bounded.** Input is length-capped before any regex runs, so a pathological paste cannot
  turn the live-preview endpoint into a CPU sink.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.utils import timezone

MAX_INPUT_LENGTH = 2000
MAX_TITLE_LENGTH = 240
MAX_LABELS = 10

TIME_RE = re.compile(
    r"(?<![\w:])(?P<hour>1[0-2]|0?[1-9])(?::(?P<minute>[0-5][0-9]))?\s*(?P<period>am|pm)(?!\w)",
    re.I,
)
TIME_24H_RE = re.compile(r"(?<![\w:])(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)(?![\w:])")
PRIORITY_RE = re.compile(r"(?:^|\s)!(?P<priority>[1-4])(?!\w)")
LABEL_RE = re.compile(r"(?:^|\s)#(?P<label>[A-Za-z0-9_-]{1,40})(?!\w)")
MENTION_RE = re.compile(r"(?:^|\s)@(?P<mention>[A-Za-z0-9_.-]{1,60})(?!\w)")
IN_DAYS_RE = re.compile(
    r"\bin\s+(?P<amount>\d{1,3})\s+(?P<unit>day|days|week|weeks|month|months)\b", re.I
)
ON_DATE_RE = re.compile(r"\b(?P<year>20\d{2})-(?P<month>0?[1-9]|1[0-2])-(?P<day>0?[1-9]|[12]\d|3[01])\b")

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

RECURRENCE_WORDS = {
    "daily": "daily",
    "everyday": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
    "yearly": "yearly",
    "annually": "yearly",
}

DEFAULT_DUE_TIME = time(9, 0)
DEFAULT_PRIORITY = 2


@dataclass
class QuickAddResult:
    original: str
    title: str
    due_date: date | None = None
    due_time: time | None = None
    priority: int = DEFAULT_PRIORITY
    labels: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    recurrence: str = ""

    @property
    def has_due(self) -> bool:
        return bool(self.due_date or self.due_time)


def _strip(pattern: re.Pattern, text: str) -> str:
    return pattern.sub(" ", text)


def parse_quick_add(raw: str, *, now=None) -> QuickAddResult:
    now = now or timezone.localtime()
    original = raw or ""
    # Cap before any regex work: unbounded input against several backtracking-capable
    # patterns is a cheap denial-of-service on the live-preview endpoint.
    text = " ".join(original[:MAX_INPUT_LENGTH].strip().split())

    labels: list[str] = []
    for match in LABEL_RE.finditer(text):
        label = match.group("label")
        if label.lower() not in {existing.lower() for existing in labels}:
            labels.append(label)
        if len(labels) >= MAX_LABELS:
            break

    mentions = list(dict.fromkeys(MENTION_RE.findall(text)))

    priority_match = PRIORITY_RE.search(text)
    priority = int(priority_match.group("priority")) if priority_match else DEFAULT_PRIORITY

    text = _strip(LABEL_RE, text)
    text = _strip(MENTION_RE, text)
    text = _strip(PRIORITY_RE, text)

    recurrence = ""
    for word, canonical in RECURRENCE_WORDS.items():
        pattern = re.compile(rf"\b{word}\b", re.I)
        if pattern.search(text):
            recurrence = canonical
            text = pattern.sub(" ", text)
            break

    due_date, text = _extract_date(text, now)
    due_time, text = _extract_time(text)

    title = " ".join(text.split()).strip()
    if not title:
        # Everything was consumed by modifiers. Fall back to the raw input rather than
        # creating a task the user cannot recognise.
        title = " ".join(original.split()).strip() or "Untitled task"
    title = title[:MAX_TITLE_LENGTH]

    return QuickAddResult(
        original=original,
        title=title,
        due_date=due_date,
        due_time=due_time,
        priority=priority,
        labels=labels,
        mentions=mentions,
        recurrence=recurrence,
    )


def _extract_date(text: str, now) -> tuple[date | None, str]:
    """Resolve the first date expression. Order matters: specific before generic."""

    explicit = ON_DATE_RE.search(text)
    if explicit:
        try:
            found = date(
                int(explicit.group("year")), int(explicit.group("month")), int(explicit.group("day"))
            )
        except ValueError:
            # e.g. 2026-02-31 — leave it in the title rather than guessing.
            found = None
        if found is not None:
            return found, text[: explicit.start()] + " " + text[explicit.end() :]

    relative = IN_DAYS_RE.search(text)
    if relative:
        amount = int(relative.group("amount"))
        unit = relative.group("unit").lower()
        days = amount * {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30}[unit]
        found = (now + timedelta(days=days)).date()
        return found, text[: relative.start()] + " " + text[relative.end() :]

    for word, delta in (("today", 0), ("tomorrow", 1), ("yesterday", -1)):
        pattern = re.compile(rf"\b{word}\b", re.I)
        if pattern.search(text):
            return (now + timedelta(days=delta)).date(), pattern.sub(" ", text)

    for name, weekday in WEEKDAYS.items():
        pattern = re.compile(rf"\b(?:next\s+)?{name}\b", re.I)
        match = pattern.search(text)
        if match:
            # "friday" means the next occurrence, never today — a task due "friday" typed on
            # a Friday almost always means the following week.
            ahead = (weekday - now.weekday()) % 7 or 7
            return (now + timedelta(days=ahead)).date(), pattern.sub(" ", text)

    return None, text


def _extract_time(text: str) -> tuple[time | None, str]:
    match = TIME_RE.search(text)
    if match:
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        period = match.group("period").lower()
        if period == "pm" and hour != 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
        return time(hour, minute), text[: match.start()] + " " + text[match.end() :]

    match = TIME_24H_RE.search(text)
    if match:
        return (
            time(int(match.group("hour")), int(match.group("minute"))),
            text[: match.start()] + " " + text[match.end() :],
        )

    return None, text


def combine_due_datetime(parsed: QuickAddResult, *, tz=None):
    """Build the aware datetime a reminder should fire against.

    Uses ``zoneinfo`` attachment rather than ``make_aware`` so ambiguous and non-existent
    local times at DST boundaries resolve by fold rules instead of raising — a user typing
    "2:30am" on a spring-forward night still gets a task.
    """

    if not parsed.due_date:
        return None
    tz = tz or timezone.get_current_timezone()
    naive = datetime.combine(parsed.due_date, parsed.due_time or DEFAULT_DUE_TIME)
    return naive.replace(tzinfo=tz)
