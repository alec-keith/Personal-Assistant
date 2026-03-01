"""
Calendar integration for Fantastical via iCloud CalDAV.

Fantastical stores its data in Apple Calendar, which syncs via iCloud's
CalDAV server. We connect directly to iCloud CalDAV so changes appear
instantly in Fantastical on your Mac/iPhone.

Also supports adding events via Fantastical's URL scheme (opens the app)
as a fallback.
"""

import logging
import subprocess
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import caldav
from caldav.elements import dav
from icalendar import Calendar, Event
import uuid as uuid_lib

from config import settings

logger = logging.getLogger(__name__)

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"


class CalendarClient:
    def __init__(self) -> None:
        self._tz = ZoneInfo(settings.agent_timezone)
        self._principal: caldav.Principal | None = None
        self._calendar: caldav.Calendar | None = None

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
            # Pick the default calendar (usually "Home")
            calendars = self._principal.calendars()
            if not calendars:
                raise RuntimeError("No calendars found on iCloud account")
            # Prefer a calendar named "Home" or just take the first
            self._calendar = next(
                (c for c in calendars if "home" in c.name.lower()),
                calendars[0],
            )
            logger.info("Connected to iCloud CalDAV, using calendar: %s", self._calendar.name)
        except Exception:
            logger.exception("Failed to connect to iCloud CalDAV")

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def list_events(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        days_ahead: int = 7,
    ) -> list[dict]:
        """Return events within a time range."""
        if self._calendar is None:
            return []

        now = datetime.now(self._tz)
        start = start or now
        end = end or (now + timedelta(days=days_ahead))

        try:
            results = self._calendar.date_search(start=start, end=end, expand=True)
            events = []
            for vevent in results:
                parsed = _parse_vevent(vevent.icalendar_component)
                if parsed:
                    events.append(parsed)
            return sorted(events, key=lambda e: e["start"])
        except Exception:
            logger.exception("Failed to fetch calendar events")
            return []

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
        all_day: bool = False,
    ) -> dict:
        """Add an event to the calendar (syncs to Fantastical automatically)."""
        if self._calendar is None:
            # Fallback: open Fantastical via URL scheme
            return self._add_via_url_scheme(title, start, end, description)

        if end is None:
            end = start + timedelta(hours=1)

        cal = Calendar()
        cal.add("prodid", "-//Atlas Personal Assistant//EN")
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

        cal.add_component(event)
        ical_str = cal.to_ical().decode("utf-8")

        try:
            self._calendar.add_event(ical_str)
            logger.info("Added calendar event: %s at %s", title, start)
            return {"title": title, "start": start.isoformat(), "end": end.isoformat()}
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
        """Open Fantastical with a pre-filled event (requires GUI, Mac only)."""
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
            logger.info("Opened Fantastical URL scheme for: %s", title)
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
            lines.append(f"- {e['title']} @ {start_str}")
            if e.get("location"):
                lines[-1] += f" [{e['location']}]"
        return "\n".join(lines)

    async def list_available_calendars(self) -> list[str]:
        if self._principal is None:
            return []
        try:
            return [c.name for c in self._principal.calendars()]
        except Exception:
            return []


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _parse_vevent(component: Any) -> dict | None:
    """Extract a flat dict from an icalendar VEVENT component."""
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
            }
    except Exception:
        logger.debug("Could not parse vevent", exc_info=True)
    return None


def _fmt_dt(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%a %b %-d, %-I:%M %p")
    return str(dt)
