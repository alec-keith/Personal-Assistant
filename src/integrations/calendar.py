"""
Calendar integration for Fantastical via iCloud CalDAV.

Reads from ALL calendars, writes to the appropriate one based on context.
Every event created includes the full default alert stack.
"""

import logging
import subprocess
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import caldav
from icalendar import Calendar, Event, Alarm
import uuid as uuid_lib

from config import settings

logger = logging.getLogger(__name__)

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"

# Default alert offsets (negative = before event)
DEFAULT_ALERTS = [
    timedelta(weeks=-2),    # 2 weeks before
    timedelta(days=-7),     # 1 week before
    timedelta(days=-3),     # 3 days before
    timedelta(days=-1),     # 1 day before
    timedelta(hours=-1),    # 1 hour before
    timedelta(minutes=-15), # 15 minutes before
]

# Calendars to skip when reading (iCloud noise)
SKIP_CALENDARS = {"reminders", "siri"}


class CalendarClient:
    def __init__(self) -> None:
        self._tz = ZoneInfo(settings.agent_timezone)
        self._principal: caldav.Principal | None = None
        # All writable calendars by lowercase name
        self._calendars: dict[str, caldav.Calendar] = {}

        if settings.use_icloud:
            self._connect()

    def _connect(self) -> None:
        try:
            client = caldav.DAVClient(
                url=ICLOUD_CALDAV_URL,
                username=settings.icloud_username,
                password=settings.icloud_app_password,
            )
            self._principal = client.principal()
            all_cals = self._principal.calendars()
            if not all_cals:
                raise RuntimeError("No calendars found on iCloud account")

            for c in all_cals:
                name_lower = c.name.lower().strip()
                if any(skip in name_lower for skip in SKIP_CALENDARS):
                    continue
                self._calendars[name_lower] = c

            names = list(self._calendars.keys())
            logger.info("Connected to iCloud CalDAV — calendars: %s", names)
        except Exception:
            logger.exception("Failed to connect to iCloud CalDAV")

    # ------------------------------------------------------------------
    # Calendar lookup
    # ------------------------------------------------------------------

    def get_calendar(self, name: str | None = None) -> caldav.Calendar | None:
        """
        Return the caldav.Calendar for the given name (case-insensitive).
        Falls back to 'home', then first available.
        """
        if not self._calendars:
            return None
        if name:
            cal = self._calendars.get(name.lower().strip())
            if cal:
                return cal
            # Fuzzy: find first calendar whose name contains the search term
            for key, cal in self._calendars.items():
                if name.lower() in key:
                    return cal
        # Default preference: home > first
        return self._calendars.get("home") or next(iter(self._calendars.values()))

    def calendar_names(self) -> list[str]:
        """Return display names of all available calendars."""
        return [c.name for c in self._calendars.values()]

    # ------------------------------------------------------------------
    # Read — all calendars
    # ------------------------------------------------------------------

    async def list_events(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        days_ahead: int = 7,
        calendar_name: str | None = None,
    ) -> list[dict]:
        """Return events across all calendars (or a specific one) within a time range."""
        if not self._calendars:
            return []

        now = datetime.now(self._tz)
        start = start or now
        end = end or (now + timedelta(days=days_ahead))

        cals_to_search = (
            [self.get_calendar(calendar_name)]
            if calendar_name
            else list(self._calendars.values())
        )

        events: list[dict] = []
        for cal in cals_to_search:
            if cal is None:
                continue
            try:
                results = cal.date_search(start=start, end=end, expand=True)
                for vevent in results:
                    parsed = _parse_vevent(vevent.icalendar_component, cal.name)
                    if parsed:
                        events.append(parsed)
            except Exception:
                logger.debug("Failed to fetch events from %s", cal.name, exc_info=True)

        return sorted(events, key=lambda e: e["start"] or "")

    async def get_today_events(self) -> list[dict]:
        now = datetime.now(self._tz)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return await self.list_events(start=start, end=end)

    # ------------------------------------------------------------------
    # Write via CalDAV
    # ------------------------------------------------------------------

    async def add_event(
        self,
        title: str,
        start: datetime,
        end: datetime | None = None,
        description: str = "",
        location: str = "",
        calendar_name: str | None = None,
    ) -> dict:
        """
        Add an event to a specific calendar (defaults to Home).
        Includes the full default alert stack automatically.
        Syncs to Fantastical immediately.
        """
        cal_obj = self.get_calendar(calendar_name)
        if cal_obj is None:
            return self._add_via_url_scheme(title, start, end, description)

        if end is None:
            end = start + timedelta(hours=1)

        cal = Calendar()
        cal.add("prodid", "-//Roman Personal Assistant//EN")
        cal.add("version", "2.0")

        event = Event()
        event.add("uid", str(uuid_lib.uuid4()))
        event.add("summary", title)
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("dtstamp", datetime.now(timezone.utc))
        if description:
            event.add("description", description)
        if location:
            event.add("location", location)

        # Add all default alerts
        for offset in DEFAULT_ALERTS:
            alarm = Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", title)
            alarm.add("trigger", offset)
            event.add_component(alarm)

        cal.add_component(event)
        ical_str = cal.to_ical().decode("utf-8")

        try:
            cal_obj.add_event(ical_str)
            target = cal_obj.name
            logger.info("Added event '%s' to '%s' at %s", title, target, start)
            return {
                "title": title,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "calendar": target,
            }
        except Exception:
            logger.exception("CalDAV add_event failed, falling back to URL scheme")
            return self._add_via_url_scheme(title, start, end, description)

    def _add_via_url_scheme(
        self,
        title: str,
        start: datetime,
        end: datetime | None,
        notes: str = "",
    ) -> dict:
        """Open Fantastical with a pre-filled event (Mac only, GUI fallback)."""
        params = {
            "title": title,
            "start": start.strftime("%Y-%m-%d %H:%M"),
            "notes": notes,
        }
        if end:
            params["end"] = end.strftime("%Y-%m-%d %H:%M")
        query = urllib.parse.urlencode(params)
        url = f"fantastical2://x-callback-url/add?{query}"
        try:
            subprocess.Popen(["open", url])
        except Exception:
            logger.exception("Failed to open Fantastical URL scheme")
        return {"title": title, "start": start.isoformat(), "method": "url_scheme"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def format_events_summary(self, events: list[dict]) -> str:
        if not events:
            return "No events found."
        lines = []
        for e in events:
            start_str = e.get("start_formatted", e.get("start", "?"))
            cal_tag = f" [{e['calendar']}]" if e.get("calendar") else ""
            line = f"- {e['title']} @ {start_str}{cal_tag}"
            if e.get("location"):
                line += f" ({e['location']})"
            lines.append(line)
        return "\n".join(lines)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _parse_vevent(component: Any, calendar_name: str = "") -> dict | None:
    try:
        for sub in component.walk():
            if sub.name != "VEVENT":
                continue
            start = sub.get("dtstart")
            end = sub.get("dtend")
            return {
                "title": str(sub.get("summary", "Untitled")),
                "start": start.dt.isoformat() if start else None,
                "end": end.dt.isoformat() if end else None,
                "start_formatted": _fmt_dt(start.dt) if start else "?",
                "location": str(sub.get("location", "")),
                "description": str(sub.get("description", "")),
                "uid": str(sub.get("uid", "")),
                "calendar": calendar_name,
            }
    except Exception:
        logger.debug("Could not parse vevent", exc_info=True)
    return None


def _fmt_dt(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%a %b %-d, %-I:%M %p")
    return str(dt)
