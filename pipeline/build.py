"""Build talks.json for the University of Birmingham talks site (Phase 1).

  python3 build.py            # fetch every source, write ../site/data/talks.json
  python3 build.py --offline  # rebuild from the raw cache only

Robustness rules:
  * each series is fetched independently; one failure never stops the run
  * if a source fails, the series keeps its talks from the last good build
    and is marked "cached" with the error, so the site still shows them
  * every series gets a status: ok, quiet (no upcoming talks listed),
    cached (source failed, last good copy shown) or failed (nothing to show)
  * online meeting links are removed from descriptions before publishing
"""
import json
import os
import re
import sys
from datetime import datetime, date, timedelta

import sources

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "raw")
OUT = os.path.join(HERE, "..", "site", "data", "talks.json")
OFFLINE = "--offline" in sys.argv

cfg = json.load(open(os.path.join(HERE, "series.json")))
TERM = cfg["term"]
# Keep the whole term plus a short look-ahead into the next one.
WIN_START = TERM["start"]
# ...and never less than five months ahead, so a scheduled run keeps listing talks after the term ends.
WIN_END = max(date.fromisoformat(TERM["end"]) + timedelta(days=60), date.today() + timedelta(days=150)).isoformat()

ZOOM = re.compile(r"https?://\S*(zoom\.us|teams\.microsoft|meet\.google)\S*", re.I)


def clean(text, n=700):
    text = ZOOM.sub("[online link available from the organiser]", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def to_utc(iso):
    from zoneinfo import ZoneInfo
    from datetime import timezone
    dt = datetime.fromisoformat(iso[:19]).replace(tzinfo=ZoneInfo("Europe/London"))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_speaker(text):
    """'Name (Affiliation)' -> ('Name', 'Affiliation'); handles nested brackets."""
    text = (text or "").strip()
    if not text.endswith(")"):
        return text, ""
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        if text[i] == ")":
            depth += 1
        elif text[i] == "(":
            depth -= 1
            if depth == 0:
                name, aff = text[:i].strip(), text[i + 1:-1].strip()
                return (name, aff) if name else (text, "")
    return text, ""


def log(*a):
    print(*a, file=sys.stderr)


# ------------------------------------------------------------------ fetchers
def fetch_uob():
    cache = os.path.join(RAW, "uob_all.json")
    if OFFLINE and os.path.exists(cache):
        return json.load(open(cache))["talks"]
    os.makedirs(RAW, exist_ok=True)
    talks, failed = sources.uob_events(log=log)
    json.dump({"fetched": datetime.now().isoformat(timespec="seconds"),
               "talks": talks, "failed": failed}, open(cache, "w"), indent=1)
    if failed:
        log(f"  {len(failed)} event pages failed (kept going)")
    return talks


SPEAKER = re.compile(
    r"(?i:given by|guest speaker|with speaker|speaker:|invites|lecture by|led by|featuring)\s+"
    r"([A-Z][^.,;()]{2,60}?)"
    r"(?:\s*\(([^)]{2,80})\)|,\s*(?:from (?:the )?)?([A-Z][^.\n]{2,60})|\s+(?:for|on|to|presents|will)\b|[.;]|$)")


def find_speaker(*texts):
    for text in texts:
        m = SPEAKER.search(text or "")
        if m:
            inst = (m.group(2) or m.group(3) or "").strip()
            out = m.group(1).strip() + (f" ({inst})" if inst else "")
            while out.count(")") > out.count("("):
                out = out[::-1].replace(")", "", 1)[::-1]
            return out
    return ""


NAME = r"(?:Professor|Prof\.?|Dr\.?|Justice)\s+[A-Z][\w'’.-]+(?:\s+[A-Z][\w'’.-]+){0,3}"


def title_speaker(title, desc, body):
    """Speaker and talk title from common University event title patterns."""
    m = re.match(r"^(?:Book|Research) [Tt]alk by (.+?)(?:\s+[-–]\s+|,\s+)(.+)$", title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^Inaugural [Ll]ecture of (.+)$", title)
    if m:
        q = re.search(r"inaugural lecture\s*[\"“]\s*(.+?)\s*[\"”]", body)
        return m.group(1).strip(), (q.group(1) if q else title)
    m = re.match(r"^(.+?):\s*[Ii]naugural lecture by (.+)$", title)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    m = re.match(rf"^({NAME})$", title)
    if m:
        q = re.search(r"entitled[,:]?\s+(.+?)\.?$", desc or "")
        if q:
            return m.group(1), q.group(1).strip()
        lines = [x.strip() for x in body.split("\n") if x.strip()]
        if len(lines) > 1 and len(lines[1]) < 120:   # series sentence, then the talk title
            return m.group(1), lines[1].rstrip(".")
        return m.group(1), title
    m = re.match(rf"^(.+?):\s*({NAME})$", title)
    if m:
        return m.group(2), title
    m = re.match(r"^(.+?)\s+with\s+([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){1,3})$", title)
    if m:
        t = re.sub(r"^.*?Lecture \d{4}:\s*", "", m.group(1))
        return m.group(2), t
    for text in (desc or "", body[:400]):
        m = (re.search(rf"\b({NAME})\s+(?:shares|discusses|delivers|explores|presents|reflects)\b", text)
             or re.search(rf"\b(?:Join|with)\s+({NAME})", text)
             or re.match(r"^([A-Z][a-z]+ [A-Z][\w'’-]+) delivers\b", text))
        if m:
            return m.group(1), title
    return "", title


def uob_strong(t):
    return " ".join([t.get("title", ""), t.get("description", ""), t.get("standfirst", "")])


def from_uob(all_events, feed, all_feeds):
    """Assign University events to a series.

    A series named in the event's title, summary or standfirst is a strong match.
    The body is only used when no series is named in those fields, because
    copied event pages sometimes carry another series' body text.
    """
    pat = re.compile(feed["match"], re.I)
    others = [re.compile(f["match"], re.I) for f in all_feeds if f is not feed]
    img = feed.get("image")
    out = []
    for t in all_events:
        strong = uob_strong(t)
        body = (t.get("body") or "")[:400]
        if pat.search(strong):
            hit = True
        elif any(o.search(strong) for o in others):
            hit = False
        else:
            hit = bool(pat.search(body)) or bool(img and img in (t.get("image") or ""))
        if not hit:
            continue
        if feed.get("exclude") and re.search(feed["exclude"], strong, re.I):
            continue
        title = re.sub(r"^Lapworth Lecture\s*[-–:]\s*", "", t["title"])
        speaker = find_speaker(t.get("description"), t.get("standfirst"), body)
        desc = t.get("description") or ""
        if not speaker:
            speaker, title = title_speaker(title, desc, t.get("body") or "")
        # Some pages use the series name or the speaker's name as the event title;
        # the real talk title is then in the summary.
        if re.search(r"seminar series$", title, re.I) or (speaker and title in speaker):
            m = (re.match(r"^(.+?),\s*([^-–]+?)\s+[-–]\s+['‘\"](.+?)['’\"]\s*$", desc)
                 or re.match(r"^.*?\bby\s+(.+?)():\s*(.+?)\.?$", desc))
            if m:
                speaker = m.group(1).strip() + (f" ({m.group(2).strip()})" if m.group(2) else "")
                title = m.group(3).strip()
        out.append({
            "title": title, "speaker": speaker,
            "start": t["start"], "end": t["end"],
            "location": t.get("location", ""), "online": t.get("online", False),
            "description": t.get("description") or t.get("body", ""),
            "url": t["url"], "cancelled": t.get("status") == "EventCancelled"
            or t["title"].lower().startswith("cancelled"),
        })
    return out


def from_researchseminars(url):
    out = []
    for t in sources.ics(url, "researchseminars"):
        d = t["description"]
        title = re.search(r"Title:\s*(?:<a[^>]*>)?(.*?)(?:</a>)?\n", d)
        room = re.search(r"Lecture held in ([^\n]+?)\.?\n", d)
        abstract = d.split("Abstract", 1)[1] if "Abstract" in d else ""
        out.append({
            "title": sources.strip_tags(title.group(1)) if title else "Title to be announced",
            "speaker": t["title"], "start": t["start"], "end": t["end"],
            "location": room.group(1).strip() if room else "",
            "online": "livestream" in d.lower(),
            "description": abstract, "url": t["location"] or url, "cancelled": False,
        })
    return out


def from_indico(url):
    out = []
    for t in sources.ics(url, "indico"):
        if t["title"].strip().lower() in ("vacant", "tbc", "no seminar"):
            continue
        d = t["description"]
        sp = re.search(r"Speakers?:\s*([^\n]+)", d)
        desc = re.sub(r"^Speakers?:[^\n]*\n?", "", d).strip()
        if desc.startswith("http"):
            desc = ""
        out.append({
            "title": t["title"], "speaker": sp.group(1).strip() if sp else "",
            "start": t["start"], "end": t["end"], "location": t["location"],
            "online": False, "description": desc, "url": t["url"], "cancelled": False,
        })
    return out


def from_theory(url):
    return [dict(t, online=False, cancelled=False) for t in sources.theory_physics(url)]


# ------------------------------------------------------------------ build
def main():
    previous = {}
    if os.path.exists(OUT):
        old = json.load(open(OUT))
        for t in old.get("talks", []):
            previous.setdefault(t["series"], []).append(t)

    uob_all = None
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Europe/London")).replace(microsecond=0, tzinfo=None)  # London wall clock
    generated = to_utc(now.isoformat())
    cache = os.path.join(RAW, "uob_all.json")
    if OFFLINE and os.path.exists(cache):  # rebuilding from cache: report when the data was collected
        generated = to_utc(json.load(open(cache))["fetched"])
    series_out, talks_out = [], []

    for s in cfg["series"]:
        f = s["feed"]
        status, error, got = "ok", "", []
        try:
            if f["type"] == "uob":
                if uob_all is None:
                    uob_all = fetch_uob()
                got = from_uob(uob_all, f, [x["feed"] for x in cfg["series"] if x["feed"]["type"] == "uob"])
            elif f["type"] == "researchseminars":
                got = from_researchseminars(f["url"])
            elif f["type"] == "indico":
                got = from_indico(f["url"])
            elif f["type"] == "theory":
                got = from_theory(f["url"])
            elif f["type"] == "craven":
                got = [dict(t, online=False, cancelled=False) for t in sources.craven_listing(f["url"])]
            elif f["type"] == "iop":
                got = [dict(t, online=False, cancelled=False) for t in sources.iop_lectures(f["url"])]
            elif f["type"] == "page":
                status = "page"   # talks are only on the series' own page; nothing to read
        except Exception as e:  # keep last good copy
            error = f"{type(e).__name__}: {e}"
            got = previous.get(s["id"], [])
            status = "cached" if got else "failed"

        # window, dedupe, tidy; when the same event appears twice, prefer the copy whose address matches its title
        def slug(x):
            return re.sub(r"[^a-z0-9]+", "-", x.lower()).strip("-")[:20]
        got = sorted(got, key=lambda t: 0 if slug(t["title"]) in t.get("url", "") else 1)
        seen, kept = set(), []
        for t in got:
            if not (WIN_START <= t["start"][:10] <= WIN_END):
                continue
            key = (t["start"][:16], re.sub(r"\W+", "", t["title"].lower())[:40])
            if key in seen:
                continue
            seen.add(key)
            t = dict(t)
            t["series"] = s["id"]
            t["description"] = clean(t.get("description", ""))
            t["speaker"], t["speaker_affiliation"] = split_speaker(t.get("speaker", ""))
            t["source_key"] = t["url"] + "#" + t["start"][:16]   # stable id for de-duplication in the database
            t["startUtc"] = to_utc(t["start"])
            t["endUtc"] = to_utc(t["end"]) if t.get("end") else ""
            t["id"] = f"{s['id']}-{t['start'][:16].replace(':', '').replace('-', '')}"
            kept.append(t)
        kept.sort(key=lambda t: t["start"])

        upcoming = [t for t in kept if t["start"] >= now.isoformat()[:19] and not t.get("cancelled")]
        if status == "ok" and not upcoming:
            status = "quiet"
        series_out.append({
            **{k: s[k] for k in ("id", "name", "school", "college", "organiser", "page", "themes")},
            "short": s.get("short", s["name"]),
            "entry": s.get("entry", "feed"),
            "feedType": f["type"], "feedUrl": f.get("url", "https://www.birmingham.ac.uk/events/sitemap.xml"),
            "status": status, "error": error,
            "termCount": sum(1 for t in kept if t["start"][:10] <= TERM["end"] and not t.get("cancelled")),
            "next": upcoming[0]["start"] if upcoming else None,
        })
        talks_out.extend(kept)
        log(f"{s['id']:9} {status:7} {len(kept):3} talks  {error}")

    # two talks at the same time in one series (for example a cancelled talk and its replacement)
    # would share an id: give the later or cancelled one a suffix
    used = {}
    for t in sorted(talks_out, key=lambda t: (t["id"], bool(t.get("cancelled")))):
        n = used.get(t["id"], 0)
        used[t["id"]] = n + 1
        if n:
            t["id"] = f"{t['id']}-{n + 1}"
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"generated": generated, "term": TERM, "siteUrl": cfg.get("siteUrl", ""), "calendarBase": cfg.get("calendarBase", ""),
               "series": series_out, "talks": sorted(talks_out, key=lambda t: t["start"])},
              open(OUT, "w"), indent=1, ensure_ascii=False)
    log(f"wrote {OUT}: {len(talks_out)} talks")


if __name__ == "__main__":
    main()
