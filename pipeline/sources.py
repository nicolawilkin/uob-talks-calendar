"""Source adapters for the University of Birmingham talks aggregator (Phase 1).

Each adapter returns a list of raw talk dicts:
  {title, start, end, location, description, url, source, speaker?, online?}
Times are naive local times in Europe/London, ISO 8601 strings.

Only public, published formats are used:
  - schema.org Event JSON-LD on www.birmingham.ac.uk event pages, discovered
    through the public events sitemap (allowed by robots.txt)
  - iCalendar feeds (researchseminars.org, Indico)
  - plain HTML seminar listings on group web pages
"""
import html
import json
import re
import time
import urllib.request
from datetime import datetime

UA = "UoB-Talks-Aggregator/0.1 (prototype; contact: talks pilot)"
DELAY = 0.4  # seconds between requests to the same host


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_tags(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t\r\f\v]+", " ", s).strip()


# ---------------------------------------------------------------- UoB events
def uob_event_urls():
    xml = get("https://www.birmingham.ac.uk/events/sitemap.xml")
    urls = re.findall(r"<loc>([^<]+)</loc>", xml)
    return [u for u in urls if re.match(r"https://www\.birmingham\.ac\.uk/events/[^/]+$", u)]


def uob_event(url):
    page = get(url)
    out = []
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(block)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for d in items:
            if d.get("@type") != "Event":
                continue
            loc = d.get("location") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            place = loc.get("name") or ""
            addr = loc.get("address")
            if isinstance(addr, dict):
                addr = ", ".join(v for k, v in addr.items() if k != "@type" and isinstance(v, str))
            mode = d.get("eventAttendanceMode", "")
            out.append({
                "title": html.unescape(d.get("name", "")).strip(),
                "start": (d.get("startDate") or "")[:19],
                "end": (d.get("endDate") or "")[:19],
                "location": html.unescape(place or (addr or "")).strip(),
                "description": strip_tags(d.get("description", "")),
                "url": url,
                "source": "uob-jsonld",
                "online": "Mixed" in mode or "Online" in mode,
                "image": d.get("image", "") if isinstance(d.get("image"), str) else "",
                "status": d.get("eventStatus", "").rsplit("/", 1)[-1],
            })
    # The page body usually names the series ("Africa Talks seminar given by ...",
    # "Classics, Ancient History and Archaeology research seminar series 2026").
    body = " ".join(strip_tags(b) for b in re.findall(
        r'<div class="rich-text__container">(.*?)</div>', page, re.S))
    head = re.search(r'<h1[^>]*>(.*?)</h1>\s*(?:<[^>]+>\s*)*<p[^>]*>(.*?)</p>', page, re.S)
    standfirst = strip_tags(head.group(2)) if head else ""
    for t in out:
        t["body"] = body[:2000]
        t["standfirst"] = standfirst[:400]
    return out


def uob_events(limit=None, log=print):
    urls = uob_event_urls()
    if limit:
        urls = urls[:limit]
    talks, failed = [], []
    for i, u in enumerate(urls):
        try:
            talks.extend(uob_event(u))
        except Exception as e:  # keep going; one bad page must not stop the run
            failed.append((u, str(e)))
        time.sleep(DELAY)
        if (i + 1) % 50 == 0:
            log(f"  uob events: {i+1}/{len(urls)}")
    return talks, failed


# ---------------------------------------------------------------- iCalendar
def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def _ics_time(v):
    v = v.split(":")[-1]
    if v.endswith("Z"):
        from datetime import timezone
        from zoneinfo import ZoneInfo
        dt = datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("Europe/London")).replace(tzinfo=None).isoformat()
    if "T" in v:
        return datetime.strptime(v[:15], "%Y%m%dT%H%M%S").isoformat()
    return datetime.strptime(v[:8], "%Y%m%d").isoformat()


def _ics_text(v):
    return v.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def ics(url, source):
    text = _unfold(get(url))
    talks = []
    for ev in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        f = {}
        for line in ev.strip().splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            key = k.split(";")[0].upper()
            f[key] = (k, v)
        if "DTSTART" not in f:
            continue
        k, v = f["DTSTART"]
        talks.append({
            "title": _ics_text(f.get("SUMMARY", ("", ""))[1]).strip(),
            "start": _ics_time(k + ":" + v) if "TZID" not in k else _ics_time(v),
            "end": _ics_time(":".join(f["DTEND"])) if "DTEND" in f else "",
            "location": _ics_text(f.get("LOCATION", ("", ""))[1]).strip(),
            "description": _ics_text(f.get("DESCRIPTION", ("", ""))[1]).strip()[:1500],
            "url": _ics_text(f.get("URL", ("", url))[1]).strip() or url,
            "source": source,
        })
    return talks


# ---------------------------------------------------------------- HTML lists
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def craven_listing(url, tries=4):
    """Retry wrapper: the School of Mathematics server often drops connections."""
    import time
    for n in range(tries):
        try:
            return _craven_listing(url)
        except Exception:
            if n == tries - 1:
                raise
            time.sleep(2 + 2 * n)


def _craven_listing(url):
    """School of Mathematics seminar pages (web.mat.bham.ac.uk/D.A.Craven/seminars/...).

    Each talk is a block: title, speaker line, 'Weekday D Month YYYY, HH:MM-HH:MM', room.
    """
    page = get(url)
    text = strip_tags(re.sub(r"</(p|h\d|div|li)>", "\n", page, flags=re.I))
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    pat = re.compile(rf"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) (\d{{1,2}}) ({MONTHS}) (\d{{4}}), (\d{{1,2}}):(\d{{2}})-(\d{{1,2}}):(\d{{2}})")
    talks = []
    for i, l in enumerate(lines):
        m = pat.match(l)
        if not m:
            continue
        _, d, mon, y, h1, m1, h2, m2 = m.groups()
        day = datetime.strptime(f"{d} {mon} {y}", "%d %B %Y")
        speaker = lines[i - 1] if i >= 1 else ""
        title = lines[i - 2] if i >= 2 else ""
        if pat.match(title) or title.lower().startswith(("semester", "abstract")) or re.match(r"(?i)^title:\s*tba\.?$", title):
            title = "Title to be announced"
        title = re.sub(r"(?i)^title:\s*", "", title)
        if "," in speaker and "(" not in speaker:   # "Name, Institution" -> "Name (Institution)"
            name, aff = speaker.split(",", 1)
            speaker = f"{name.strip()} ({aff.strip()})"
        room = lines[i + 1] if i + 1 < len(lines) else ""
        if room.lower().startswith("abstract"):
            room = ""
        talks.append({
            "title": title,
            "speaker": speaker,
            "start": day.replace(hour=int(h1), minute=int(m1)).isoformat(),
            "end": day.replace(hour=int(h2), minute=int(m2)).isoformat(),
            "location": room,
            "description": "",
            "url": url,
            "source": "html-maths",
        })
    if not talks and "Semester" not in text:   # a cut-off or error page: let the build keep the last good copy
        raise ValueError("seminar page looks incomplete")
    # the same slot listed twice, once as "to be announced": keep the one with a title
    titled = {t["start"] for t in talks if t["title"] != "Title to be announced"}
    return [t for t in talks if t["title"] != "Title to be announced" or t["start"] not in titled]


def theory_physics(url="https://more.bham.ac.uk/theoretical-physics/talks/", tries=3):
    import time
    for n in range(tries):
        try:
            return _theory_physics(url)
        except Exception:
            if n == tries - 1:
                raise
            time.sleep(2 + 2 * n)


def _theory_physics(url):
    """Theoretical Physics talks page: 'Thursday 12th November 2026: Name (Institution)' then title."""
    page = get(url)
    text = strip_tags(re.sub(r"</(p|h\d|div|li)>", "\n", page, flags=re.I))
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    pat = re.compile(rf"^(?:Monday|Tuesday|Wednesday|Thursday|Friday) (\d{{1,2}})(?:st|nd|rd|th) ({MONTHS}) (\d{{4}}):\s*(.+)$")
    talks = []
    for i, l in enumerate(lines):
        m = pat.match(l)
        if not m:
            continue
        d, mon, y, who = m.groups()
        day = datetime.strptime(f"{d} {mon} {y}", "%d %B %Y")
        title = lines[i + 1] if i + 1 < len(lines) else ""
        if pat.match(title):
            title = ""
        talks.append({
            "title": title if title and title != "TBA" else "Title to be announced",
            "speaker": who,
            "start": day.replace(hour=13, minute=15).isoformat(),
            "end": day.replace(hour=14, minute=30).isoformat(),
            "location": "Theory Library, 4th floor, Physics East",
            "description": "",
            "url": url,
            "source": "html-theory",
        })
    return talks


def iop_lectures(url):
    """Institute of Physics public lectures page: an <h2> date, a <strong> title, then 'Lecture by ...'."""
    page = get(url)
    blocks = re.split(r"<h2>", page)
    pat = re.compile(rf"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) (\d{{1,2}}) ({MONTHS}) (\d{{4}})</h2>")
    talks = []
    for b in blocks:
        m = pat.match(b.strip())
        if not m:
            continue
        d, mon, y = m.groups()
        day = datetime.strptime(f"{d} {mon} {y}", "%d %B %Y")
        body = b[m.end():].split("</span>")[0]
        t = re.search(r"<strong>(.*?)</strong>", body, re.S)
        title = strip_tags(t.group(1)).strip() if t else "Title to be announced"
        paras = [strip_tags(x).strip() for x in re.findall(r"<p>(.*?)</p>", body, re.S)]
        who = ""
        for x in paras:
            w = re.match(r"Lecture by:?\s*(.+)", x)
            if w:
                who = re.split(r"\s+[-–]\s+", w.group(1))[0].strip()
        abstract = " ".join(x for x in paras if x and x != title and not x.startswith("Lecture by"))
        talks.append({
            "title": title, "speaker": who,
            "start": day.replace(hour=19, minute=30).isoformat(),
            "end": day.replace(hour=20, minute=30).isoformat(),
            "location": "Poynting Large Lecture Theatre, Physics Building (R13)",
            "description": abstract, "url": url, "source": "html-iop",
        })
    return talks
