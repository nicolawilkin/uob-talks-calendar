"""Write calendar files (.ics) from site/data/talks.json.

Output, in calendar/ (published as a static site, for example on GitHub Pages):
  talks/<talk id>.ics     one talk, for "download a calendar file"
  series/<series id>.ics  every talk in a series, for subscribing
  all.ics                 every talk on the site
  index.html              a plain page listing the subscription feeds

Usage: python3 calendars.py [path/to/talks.json] [output folder]
"""
import json
import os
import sys
from datetime import datetime, timezone
from html import escape

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "site", "data", "talks.json")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "..", "calendar")
PRODID = "-//University of Birmingham Talks//Prototype//EN"


def text(value):
    """Escape a value for an iCalendar TEXT property (RFC 5545, 3.3.11)."""
    return (str(value or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n"))


def fold(line):
    """Fold a content line at 75 octets without splitting a UTF-8 character."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def stamp(iso_utc):
    return iso_utc.replace("-", "").replace(":", "")[:15] + "Z"


def event(t, series, now):
    who = t.get("speaker", "")
    if t.get("speaker_affiliation"):
        who += f" ({t['speaker_affiliation']})"
    desc = []
    if who:
        desc.append(who)
    desc.append(series["name"])
    if t.get("description"):
        desc.append("")
        desc.append(t["description"])
    if t.get("url"):
        desc.append("")
        desc.append("Details: " + t["url"])
    lines = [
        "BEGIN:VEVENT",
        f"UID:{t['id']}@talks.birmingham.prototype",
        f"DTSTAMP:{now}",
        f"DTSTART:{stamp(t['startUtc'])}",
        f"DTEND:{stamp(t['endUtc'] or t['startUtc'])}",
        "SUMMARY:" + text(("Cancelled: " if t.get("cancelled") and not t["title"].lower().startswith("cancelled") else "")
                          + t["title"] + (f" ({who})" if who else "")),
        "LOCATION:" + text(t.get("location") or ("Online" if t.get("online") else "")),
        "DESCRIPTION:" + text("\n".join(desc)),
        "CATEGORIES:" + text(series["name"]),
        "STATUS:" + ("CANCELLED" if t.get("cancelled") else "CONFIRMED"),
    ]
    if t.get("url", "").startswith("https://"):
        lines.append("URL:" + t["url"])
    lines.append("END:VEVENT")
    return lines


def calendar(name, events):
    head = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH", "X-WR-CALNAME:" + text(name), "X-WR-TIMEZONE:Europe/London",
            "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H"]
    body = [line for ev in events for line in ev]
    return "\r\n".join(fold(l) for l in head + body + ["END:VCALENDAR"]) + "\r\n"


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def main():
    data = json.load(open(SRC, encoding="utf-8"))
    series = {s["id"]: s for s in data["series"]}
    # DTSTAMP is the collection time, so a rerun without changes rewrites identical files
    now = stamp(data.get("generated") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    talks = [t for t in data["talks"] if t.get("startUtc") and t["series"] in series]

    # clear old talk files so withdrawn talks disappear
    tdir = os.path.join(OUT, "talks")
    if os.path.isdir(tdir):
        for f in os.listdir(tdir):
            if f.endswith(".ics"):
                os.remove(os.path.join(tdir, f))

    by_series = {}
    for t in talks:
        ev = event(t, series[t["series"]], now)
        write(os.path.join(tdir, t["id"] + ".ics"), calendar(t["title"], [ev]))
        by_series.setdefault(t["series"], []).append(ev)

    for sid, s in series.items():
        write(os.path.join(OUT, "series", sid + ".ics"),
              calendar(s["name"] + " (University of Birmingham)", by_series.get(sid, [])))
    write(os.path.join(OUT, "all.ics"),
          calendar("Talks at the University of Birmingham", [e for sid in by_series for e in by_series[sid]]))

    rows = "\n".join(
        f'<li><a href="series/{escape(sid)}.ics">{escape(s["name"])}</a></li>'
        for sid, s in sorted(series.items(), key=lambda x: x[1]["name"].lower()))
    site = escape(data.get("siteUrl", ""))
    write(os.path.join(OUT, "index.html"), f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Talks calendars: University of Birmingham</title>
<style>body{{font:16px/1.55 Arial,sans-serif;color:#1B1B1B;background:#FAFAFA;max-width:760px;margin:40px auto;padding:0 20px}}
h1{{font-family:'Times New Roman',serif}}a{{color:#1B1B1B}}li{{margin:4px 0}}</style></head>
<body><h1>Talks calendars</h1>
<p>Calendar files for the University of Birmingham talks prototype{f' (<a href="{site}">open the site</a>)' if site else ''}.
To follow a series, copy its link below and add it to your calendar as a subscription (in Outlook: Add calendar, then Subscribe from web).
The files are rebuilt whenever the talks are collected.</p>
<p><a href="all.ics">All talks</a></p>
<h2>By series</h2>
<ul>
{rows}
</ul>
</body></html>
""")
    write(os.path.join(OUT, ".nojekyll"), "")
    print(f"wrote {len(talks)} talk files, {len(series)} series feeds and all.ics to {OUT}")


if __name__ == "__main__":
    main()
