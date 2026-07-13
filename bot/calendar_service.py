import asyncio
import os
import re
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# httplib2.Http is not thread-safe — serialize all calendar fetches through one thread
_CAL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cal-fetch")

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "/credentials/credentials.json")
TOKEN_FILE = os.environ.get("GOOGLE_TOKEN_FILE", "/credentials/token.json")


class CalendarService:
    def __init__(self, config: dict):
        self.config = config
        self.tz = ZoneInfo(config["timezone"])
        # Build calendar → members map
        self._cal_to_members: dict[str, list[str]] = {}
        for name, member in config["family_members"].items():
            for cal_id in member.get("calendars", []):
                self._cal_to_members.setdefault(cal_id, []).append(name)

        # Support both old flat list and new structured format with name/alias
        shared = config.get("shared_calendars", [])
        self._cal_names: dict[str, str] = {}   # entity_id → friendly name
        self._alias_map: dict[str, str] = {}   # alias.lower() → entity_id
        for entry in shared:
            if isinstance(entry, dict):
                cal_id = entry["id"]
                self._cal_to_members.setdefault(cal_id, [])
                if entry.get("name"):
                    self._cal_names[cal_id] = entry["name"]
                for alias in entry.get("alias", []):
                    self._alias_map[alias.lower()] = cal_id
            else:
                # Legacy plain string ID
                self._cal_to_members.setdefault(entry, [])

        # Perf cache: TTL for _fetch_events (default 1800s per plan)
        self._cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
        ctx_cfg = config.get("context", {})
        self._cache_ttl = float(ctx_cfg.get("calendar_cache_ttl_s", 1800))
        self._cache_hits = 0
        self._cache_misses = 0
        self._last_calendar_cache_hit: bool | None = None

    def _get_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(TOKEN_FILE, "w") as f:
                    f.write(creds.to_json())
            else:
                raise RuntimeError("Google token missing. Run scripts/auth_google.py first.")
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    def _parse_event(self, item: dict, cal_id: str) -> dict | None:
        start_raw = item["start"].get("dateTime") or item["start"].get("date")
        end_raw   = item["end"].get("dateTime") or item["end"].get("date")
        try:
            if "T" in start_raw:
                start = datetime.fromisoformat(start_raw).astimezone(self.tz)
                end   = datetime.fromisoformat(end_raw).astimezone(self.tz)
                all_day = False
                # For timed events, due_date == start
                due_date = start
            else:
                start = datetime.strptime(start_raw, "%Y-%m-%d").replace(tzinfo=self.tz)
                end   = datetime.strptime(end_raw, "%Y-%m-%d").replace(tzinfo=self.tz)
                all_day = True
                # Google Calendar all-day end is exclusive; due_date = end - 1 day
                due_date = end - timedelta(days=1)
        except Exception:
            log.warning(f"Could not parse time for event: {item.get('summary')}")
            return None

        owners = self._cal_to_members.get(cal_id, [])
        desc = item.get("description", "") or ""

        # Parse inline reminder tag [remind:60,15]
        custom_remind = None
        if "[remind:" in desc:
            try:
                tag = desc.split("[remind:")[1].split("]")[0]
                custom_remind = [int(x.strip()) for x in tag.split(",")]
            except Exception:
                pass

        organizer = item.get("organizer", {})
        creator = item.get("creator", {})
        real_attendees = [
            {
                "email": a.get("email", ""),
                "name": a.get("displayName", ""),
                "rsvp": a.get("responseStatus", ""),
            }
            for a in item.get("attendees", [])
        ]

        return {
            "id":              item["id"],
            "summary":         item.get("summary", "(No title)"),
            "start":           start,
            "end":             end,
            "due_date":        due_date,
            "location":        item.get("location", ""),
            "description":     desc,
            "attendees":       list(owners),
            "all_day":         all_day,
            "custom_remind":   custom_remind,
            "calendar_id":     cal_id,
            "status":          item.get("status", "confirmed"),
            "organizer_name":  organizer.get("displayName", "") or organizer.get("email", ""),
            "organizer_email": organizer.get("email", ""),
            "creator_email":   creator.get("email", ""),
            "html_link":       item.get("htmlLink", ""),
            "created_at":      item.get("created", ""),
            "updated_at":      item.get("updated", ""),
            "real_attendees":  real_attendees,
            "color_id":        item.get("colorId", ""),
        }

    def _normalize_title(self, title: str) -> str:
        return re.sub(r"\s+", " ", title.lower().strip())

    def _dedup_events(self, events: list[dict]) -> list[dict]:
        """Merge events with same title and start within 15 minutes.
        Events are pre-sorted by start time, so only forward neighbours need checking."""
        merged = []
        i = 0
        while i < len(events):
            ev = events[i]
            group = [ev]
            j = i + 1
            while j < len(events):
                other = events[j]
                time_diff = (other["start"] - ev["start"]).total_seconds()
                if time_diff > 900:
                    break  # sorted order — nothing further can be within 15 min
                if self._normalize_title(ev["summary"]) == self._normalize_title(other["summary"]):
                    group.append(other)
                j += 1
            if len(group) > 1:
                seen: set[str] = set()
                merged_attendees = []
                for g in group:
                    for a in g["attendees"]:
                        if a not in seen:
                            merged_attendees.append(a)
                            seen.add(a)
                ev = dict(ev)
                ev["attendees"] = merged_attendees
                i += len(group)
            else:
                i += 1
            merged.append(ev)
        return merged

    def _fetch_events_sync(self, time_min: datetime, time_max: datetime) -> list[dict]:
        """Blocking Google Calendar fetch — always run via _fetch_events (thread pool)."""
        svc = self._get_service()
        all_events = []
        seen_ids = set()
        for cal_id in self._cal_to_members:
            try:
                page_token = None
                pages_fetched = 0
                while pages_fetched < 20:
                    pages_fetched += 1
                    result = svc.events().list(
                        calendarId=cal_id,
                        timeMin=time_min.isoformat(),
                        timeMax=time_max.isoformat(),
                        singleEvents=True,
                        orderBy="startTime",
                        maxResults=250,
                        pageToken=page_token,
                    ).execute()
                    for item in result.get("items", []):
                        if item["id"] in seen_ids:
                            continue
                        seen_ids.add(item["id"])
                        parsed = self._parse_event(item, cal_id)
                        if parsed and parsed.get("status") != "cancelled":
                            all_events.append(parsed)
                    page_token = result.get("nextPageToken")
                    if not page_token:
                        break
            except Exception as e:
                log.error(f"Error fetching calendar {cal_id}: {e}")
        all_events.sort(key=lambda e: e["start"])
        return self._dedup_events(all_events)

    async def _fetch_events(self, time_min: datetime, time_max: datetime) -> list[dict]:
        """Run the blocking Google Calendar fetch in a dedicated single-thread pool.
        Single thread avoids concurrent access to the cached httplib2.Http service object.
        1800s TTL cache per perf plan; invalidate on writes."""
        import time as _time
        key = (time_min.isoformat(), time_max.isoformat())
        now = _time.time()
        if key in self._cache:
            ts, evs = self._cache[key]
            if now - ts < self._cache_ttl:
                self._cache_hits += 1
                self._last_calendar_cache_hit = True
                return list(evs)  # copy
        self._cache_misses += 1
        self._last_calendar_cache_hit = False
        loop = asyncio.get_running_loop()
        try:
            evs = await asyncio.wait_for(
                loop.run_in_executor(_CAL_EXECUTOR, self._fetch_events_sync, time_min, time_max),
                timeout=30.0
            )
            self._cache[key] = (now, list(evs))
            return evs
        except asyncio.TimeoutError:
            log.warning("Calendar fetch timed out after 30s")
            return []

    def invalidate_calendar_cache(self):
        self._cache.clear()
        log.info("calendar cache invalidated")

    async def get_todays_events(self) -> list[dict]:
        now   = datetime.now(self.tz)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end   = start + timedelta(days=1)
        return await self._fetch_events(start, end)

    async def get_events_for_days(self, days: int) -> list[dict]:
        now   = datetime.now(self.tz)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return await self._fetch_events(start, start + timedelta(days=days))

    async def get_events_starting(self, start: datetime, days: int) -> list[dict]:
        return await self._fetch_events(start, start + timedelta(days=days))

    async def get_events_between(self, start_date: str, end_date: str) -> list[dict]:
        start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=self.tz)
        end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=self.tz) + timedelta(days=1)
        return await self._fetch_events(start, end)

    async def get_upcoming_events(self, lookahead_minutes: int) -> list[dict]:
        now = datetime.now(self.tz)
        return await self._fetch_events(now, now + timedelta(minutes=lookahead_minutes))

    async def get_historical_events(self, days_back: int = 90) -> list[dict]:
        now = datetime.now(self.tz)
        start = now - timedelta(days=days_back)
        return await self._fetch_events(start, now)

    async def get_tomorrows_events(self) -> list[dict]:
        now = datetime.now(self.tz)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return await self._fetch_events(tomorrow, tomorrow + timedelta(days=1))

    async def get_week_events_from_monday(self) -> list[dict]:
        now = datetime.now(self.tz)
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        sunday = monday + timedelta(days=7)
        return await self._fetch_events(monday, sunday)

    async def warmup(self):
        """Pre-load Google API discovery doc in a thread at startup so the first
        real fetch doesn't pay the class-generation cost on the hot path."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_CAL_EXECUTOR, self._get_service)

    def resolve_alias(self, alias: str) -> str | None:
        """Resolve a calendar alias to its ID."""
        return self._alias_map.get(alias.lower())

    async def create_event(self, summary: str, start: datetime, end: datetime,
                           attendees: list[str], location: str = "",
                           description: str = "", remind_minutes: list[int] | None = None,
                           calendar_id: str = "primary") -> dict:
        remind_tag = ""
        if remind_minutes:
            tag_str = ",".join(str(m) for m in sorted(remind_minutes, reverse=True))
            remind_tag = f"\n\n[remind:{tag_str}]"

        body = {
            "summary": summary,
            "start": {"dateTime": start.isoformat(), "timeZone": self.config["timezone"]},
            "end":   {"dateTime": end.isoformat(),   "timeZone": self.config["timezone"]},
            "description": description + remind_tag,
        }
        if location:
            body["location"] = location

        def _do_insert():
            svc = self._get_service()
            return svc.events().insert(calendarId=calendar_id, body=body).execute()

        loop = asyncio.get_running_loop()
        event = await loop.run_in_executor(None, _do_insert)
        self.invalidate_calendar_cache()
        start_dt = datetime.fromisoformat(event["start"]["dateTime"]).astimezone(self.tz)
        end_dt   = datetime.fromisoformat(event["end"]["dateTime"]).astimezone(self.tz)

        return {
            "id": event["id"], "summary": event.get("summary", ""),
            "start": start_dt, "end": end_dt,
            "location": event.get("location", ""),
            "description": event.get("description", ""),
            "attendees": attendees, "all_day": False,
            "custom_remind": remind_minutes, "calendar_id": calendar_id,
        }

    async def patch_event_description(self, calendar_id: str, event_id: str, description: str):
        def _do_patch():
            svc = self._get_service()
            svc.events().patch(
                calendarId=calendar_id,
                eventId=event_id,
                body={"description": description}
            ).execute()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _do_patch)
        self.invalidate_calendar_cache()

    def events_to_summary(self, events: list[dict], max_list: int = 6) -> str:
        """Compact JSON-ish summary for tool results when calendar_summary_mode is on."""
        import json

        if not events:
            return json.dumps({"count": 0, "next_event": None, "events": []})
        shown = events[:max_list]
        next_ev = events[0]
        payload = {
            "count": len(events),
            "next_event": {
                "summary": next_ev.get("summary"),
                "start": next_ev["start"].isoformat() if hasattr(next_ev.get("start"), "isoformat") else str(next_ev.get("start")),
            },
            "events": [
                {
                    "summary": ev.get("summary"),
                    "start": ev["start"].strftime("%Y-%m-%d %H:%M") if not ev.get("all_day") else ev["start"].strftime("%Y-%m-%d"),
                    "who": ", ".join(ev.get("attendees") or []) or "Everyone",
                }
                for ev in shown
            ],
        }
        if len(events) > max_list:
            payload["truncated"] = len(events) - max_list
        return json.dumps(payload, ensure_ascii=False)

    def events_to_text(self, events: list[dict]) -> str:
        """Convert events list to plain text for Claude's context."""
        if not events:
            return "No events found."
        lines = []
        for ev in events:
            if ev.get("all_day"):
                due = ev.get("due_date", ev["end"] - timedelta(days=1))
                if due.date() != ev["start"].date():
                    # Multi-day: show assigned → due range so Claude knows start ≠ due
                    time_str = (
                        f"{ev['start'].strftime('%A %b %d')} → due "
                        f"{due.strftime('%A %b %d')} (all day)"
                    )
                else:
                    time_str = ev["start"].strftime("%A %B %d — All day")
            else:
                time_str = (
                    ev["start"].strftime("%A %B %d, %I:%M %p %Z")
                    + " – " + ev["end"].strftime("%I:%M %p %Z")
                )
            who = ", ".join(ev["attendees"]) if ev["attendees"] else "Everyone"
            line = f"• {ev['summary']} | {time_str} | Who: {who}"
            if ev.get("location"):
                line += f" | Location: {ev['location']}"
            if ev.get("organizer_name"):
                line += f" | Organizer: {ev['organizer_name']}"
            if ev.get("status") not in ("confirmed", ""):
                line += f" | Status: {ev['status']}"
            if ev.get("html_link"):
                line += f" | Link: {ev['html_link']}"
            lines.append(line)
        return "\n".join(lines)
