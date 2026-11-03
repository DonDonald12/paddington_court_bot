#!/usr/bin/env python3
"""Paddington Rec tennis court watcher.

Runs every 5 minutes on GitHub Actions. Each run:
  1. reads any new Telegram messages and updates your wanted times
  2. checks court availability for the next few days
  3. messages you about matching slots it hasn't told you about yet

State (wanted times, slots already alerted, Telegram offset) lives in
state.json, which the workflow commits back to the repo after each run.
"""
import json
import os
import random
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/London")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "7"))
BOOKING_URL = os.environ.get("BOOKING_URL", "https://account.everyoneactive.com/book/")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DEMO = os.environ.get("DEMO", "") == "1" or "--demo" in sys.argv
FAIL_ALERT_AFTER = 6  # consecutive failed checks (about 30 min) before telling you

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_GROUPS = {
    "weekdays": [0, 1, 2, 3, 4],
    "weekday": [0, 1, 2, 3, 4],
    "weekends": [5, 6],
    "weekend": [5, 6],
    "everyday": list(range(7)),
    "daily": list(range(7)),
    "any": list(range(7)),
}

HELP = """Send me the times you want and I'll message you when a court comes free.

add sat 10-14
add weekdays 18-21
add sat,sun 9:30-12
list
remove 2
clear

A slot matches if it starts at or after the first time and before the second."""


@dataclass(frozen=True)
class Slot:
    start: datetime  # timezone-aware, Europe/London
    end: datetime
    court: str
    surface: str = "synth"  # "synth" or "tarmac"

    @property
    def key(self):
        return f"{self.start:%Y-%m-%dT%H:%M}|{self.court}"


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

LOGIN_URL = "https://api-v2.everyoneactive.com/v1/auth/login"
CONNECT = "https://book.everyoneactive.com/Connect/"
TENNIS_ACTIVITIES = {"Tennis - Synth": "synth", "Tennis - Tarmac": "tarmac"}
SLOT_MINUTES = 50
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def fetch_available(days):
    """Return every free tennis court slot at Paddington Rec on the given days."""
    if DEMO:
        return demo_slots(days)
    import requests
    from bs4 import BeautifulSoup

    email, password = os.environ.get("EA_EMAIL"), os.environ.get("EA_PASSWORD")
    if not (email and password):
        raise RuntimeError("EA_EMAIL / EA_PASSWORD secrets aren't set")

    s = requests.Session()
    s.headers["User-Agent"] = UA

    # 1. Log in to Everyone Active, which gives a token for the booking site.
    r = s.post(LOGIN_URL, json={"type": "email", "value": email, "password": password},
               headers={"Accept": "application/json"}, timeout=30)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if not r.ok or "leisureManagementToken" not in body:
        # Only the status and any error message: the logs are public.
        raise RuntimeError(f"Everyone Active login failed ({r.status_code}): "
                           f"{str(body.get('message', ''))[:150]}")
    token = body["leisureManagementToken"]

    # 2. Swap the token for a booking-site session; lands on the home page.
    r = s.get(CONNECT + "landing.aspx", params={"token": token}, timeout=30)
    r.raise_for_status()
    home = BeautifulSoup(r.text, "html.parser")
    site = home.select_one("select option[selected]")
    if not site or "Paddington" not in site.get_text():
        raise RuntimeError("Preferred site isn't Paddington Rec. Set it in the booking site.")

    def postback(page_url, soup, target):
        form = {i["name"]: i.get("value", "")
                for i in soup.select("form input[type=hidden]") if i.get("name")}
        form.update(__EVENTTARGET=target, __EVENTARGUMENT="")
        resp = s.post(page_url, data=form, timeout=30)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")

    def availability_target(soup, name):
        for a in soup.select('a[href*="lnkActivitySelect_lg"]'):
            if name in a.get_text():
                return re.search(r"'([^']+)'", a["href"]).group(1).replace(
                    "lnkActivitySelect_lg", "btnAvailability_lg")
        return None

    # Late in the day tennis can drop off today's list, so fall back to tomorrow.
    if not availability_target(home, "Tennis - Synth"):
        home = postback(CONNECT + "memberHomePage.aspx", home,
                        "ctl00$MainContent$_advanceSearchUserControl$_lnkBtnTomorrow")

    wanted_dates = set(days)
    last_day = max(days)
    slots = []
    for activity, surface in TENNIS_ACTIVITIES.items():
        target = availability_target(home, activity)
        if not target:
            raise RuntimeError(f"Couldn't find '{activity}' on the booking site")
        page = postback(CONNECT + "memberHomePage.aspx", home, target)
        seen = set()
        while True:
            grid = page.select_one("#ctl00_MainContent_grdResourceView")
            for btn in (grid.select("input[data-qa-id]") if grid else []):
                info = dict(re.findall(r"(\w+)=\s*([^=]*?)(?=\s+\w+=|$)", btn["data-qa-id"]))
                if info.get("Availability") != "Available":
                    continue
                day = datetime.strptime(info["Date"], "%Y/%m/%d").date()
                if day not in wanted_dates:
                    continue
                start = datetime.combine(day, time.fromisoformat(info["Time"]), TZ)
                slots.append(Slot(start, start + timedelta(minutes=SLOT_MINUTES),
                                  info["Court"], surface))
            nav = page.select_one("#ctl00_MainContent_lblCurrentNavDate")
            label = nav.get_text(strip=True) if nav else ""
            if not label or label in seen:
                break  # no more days the site will show
            seen.add(label)
            shown = datetime.strptime(f"{label} {date.today().year}", "%a %d %b %Y").date()
            if shown < date.today() - timedelta(days=1):  # crossed into next year
                shown = shown.replace(year=shown.year + 1)
            if shown >= last_day:
                break
            page = postback(CONNECT + "mrmProductStatus.aspx", page, "ctl00$MainContent$Button2")
    return slots


def demo_slots(days):
    """Fake availability that changes every 5 minutes, for testing alerts."""
    bucket = int(datetime.now(TZ).timestamp() // 300)
    slots = []
    for d in days:
        rng = random.Random(f"{d}-{bucket}")
        for hour in range(8, 21):
            for court in range(1, 14):
                if rng.random() < 0.03:
                    start = datetime.combine(d, time(hour), TZ)
                    surface = "tarmac" if court in (5, 6, 7) else "synth"
                    slots.append(Slot(start, start + timedelta(minutes=SLOT_MINUTES),
                                      f"Tennis Court {court}", surface))
    return slots


# ---------------------------------------------------------------------------
# Wanted times
# ---------------------------------------------------------------------------

def parse_time(text):
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?", text.strip().lower())
    if not m:
        raise ValueError(f"Couldn't read the time '{text}'")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        raise ValueError(f"Couldn't read the time '{text}'")
    return f"{hour:02d}:{minute:02d}"


def parse_days(text):
    days = set()
    for word in re.split(r"[\s,/&]+|\band\b", text.lower()):
        if not word:
            continue
        if word in DAY_GROUPS:
            days.update(DAY_GROUPS[word])
            continue
        for i, name in enumerate(DAY_NAMES):
            if word.startswith(name.lower()):
                days.add(i)
                break
        else:
            raise ValueError(f"Couldn't read the day '{word}'")
    if not days:
        raise ValueError("No days given")
    return sorted(days)


def parse_want(text):
    m = re.fullmatch(r"(.+?)\s+(\S+?)\s*(?:-|to)\s*(\S+)", text.strip(), re.I)
    if not m:
        raise ValueError("Try something like: add sat 10-14")
    days = parse_days(m.group(1))
    start, end = parse_time(m.group(2)), parse_time(m.group(3))
    if start >= end:
        raise ValueError("The first time needs to be before the second")
    return {"days": days, "from": start, "to": end}


def describe_want(w):
    days = w["days"]
    if days == DAY_GROUPS["weekdays"]:
        label = "Weekdays"
    elif days == DAY_GROUPS["weekends"]:
        label = "Weekends"
    elif days == DAY_GROUPS["daily"]:
        label = "Every day"
    else:
        label = ", ".join(DAY_NAMES[d] for d in days)
    return f"{label} {w['from']}-{w['to']}"


def matches(slot, want):
    hhmm = slot.start.strftime("%H:%M")
    return slot.start.weekday() in want["days"] and want["from"] <= hhmm < want["to"]


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg(method, **params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=20) as resp:
        return json.load(resp)


def send(text, chat_id=None):
    tg("sendMessage", chat_id=chat_id or CHAT_ID, text=text,
       disable_web_page_preview="true")


def handle_command(text, state):
    text = text.strip()
    cmd, _, rest = text.partition(" ")
    cmd = cmd.lstrip("/").split("@")[0].lower()
    wants = state["wants"]

    if cmd in ("start", "help"):
        return HELP
    if cmd == "add":
        try:
            want = parse_want(rest)
        except ValueError as e:
            return str(e)
        if want in wants:
            return "Already watching that."
        wants.append(want)
        return f"Watching {describe_want(want)}. I'll check on the next run."
    if cmd == "list":
        if not wants:
            return "Not watching anything yet. Try: add sat 10-14"
        return "\n".join(f"{i}. {describe_want(w)}" for i, w in enumerate(wants, 1))
    if cmd == "remove":
        try:
            removed = wants.pop(int(rest) - 1)
        except (ValueError, IndexError):
            return "Send 'list' to see the numbers, then e.g. 'remove 2'."
        return f"Stopped watching {describe_want(removed)}."
    if cmd == "clear":
        wants.clear()
        return "Cleared. Not watching anything."
    return "Didn't catch that.\n\n" + HELP


def handle_messages(state):
    updates = tg("getUpdates", offset=state["offset"], timeout=0).get("result", [])
    for update in updates:
        state["offset"] = update["update_id"] + 1
        msg = update.get("message") or {}
        text = msg.get("text")
        chat = str(msg.get("chat", {}).get("id", ""))
        if not text or not chat:
            continue
        if not CHAT_ID:
            send(f"Your chat ID is {chat}. Add it to GitHub as the "
                 "TELEGRAM_CHAT_ID secret.", chat_id=chat)
            continue
        if chat != CHAT_ID:
            continue  # ignore anyone else who finds the bot
        send(handle_command(text, state))


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------

def format_alert(slots):
    """One line per time. Names synth courts when any are free, else tarmac."""
    groups = {}
    for s in slots:
        groups.setdefault((s.start, s.end), []).append(s)
    lines = ["Courts free at Paddington Rec:"]
    for (start, end), group in sorted(groups.items())[:20]:
        synth = [s for s in group if s.surface == "synth"]
        pick, surface = (synth, "synth") if synth else (group, "tarmac")
        nums = sorted(int(re.sub(r"\D", "", s.court) or 0) for s in pick)
        label = "court" if len(nums) == 1 else "courts"
        courts = ", ".join(str(n) for n in nums)
        lines.append(f"{start:%a %d %b %H:%M}-{end:%H:%M}: {surface} {label} {courts}")
    if len(groups) > 20:
        lines.append(f"...and {len(groups) - 20} more")
    lines.append(f"\nBook: {BOOKING_URL}")
    return "\n".join(lines)


def check(state):
    if not state["wants"]:
        return
    now = datetime.now(TZ)
    days = [now.date() + timedelta(days=i) for i in range(DAYS_AHEAD)]
    slots = fetch_available(days)
    matching = [s for s in slots
                if s.start > now and any(matches(s, w) for w in state["wants"])]
    new = [s for s in matching if s.key not in state["notified"]]
    # Synth preferred: a newly free tarmac court isn't worth a ping if a synth
    # court is free at the same time (whether or not you've heard about it).
    synth_times = {s.start for s in matching if s.surface == "synth"}
    new = [s for s in new if s.surface == "synth" or s.start not in synth_times]
    # Forget slots that have gone, so if one is cancelled again you hear about it.
    state["notified"] = sorted(s.key for s in matching)
    if new:
        send(format_alert(new))


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    state.setdefault("wants", [])
    state.setdefault("notified", [])
    state.setdefault("offset", 0)
    state.setdefault("fail_count", 0)
    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def main():
    state = load_state()
    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set, skipping Telegram.")
    else:
        handle_messages(state)

    if CHAT_ID:
        try:
            check(state)
            state["fail_count"] = 0
        except NotImplementedError as e:
            print(e)
        except Exception as e:  # login broken, site changed, etc.
            state["fail_count"] += 1
            print(f"Check failed ({state['fail_count']} in a row): {e!r}")
            if state["fail_count"] == FAIL_ALERT_AFTER and BOT_TOKEN:
                send("Heads up: I haven't been able to check courts for about "
                     f"30 minutes. Last error: {e}")
    save_state(state)


if __name__ == "__main__":
    main()
