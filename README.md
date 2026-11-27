# Talks calendars: University of Birmingham prototype

Calendar files (.ics) for the University of Birmingham talks prototype, served by GitHub Pages at https://nicolawilkin.github.io/uob-talks-calendar/

- `talks/<talk id>.ics`: one talk
- `series/<series id>.ics`: every talk in a series, for subscribing in a calendar app
- `all.ics`: every talk on the site

## How it updates

A GitHub Action (`.github/workflows/update.yml`) runs every night at 02:17 UTC, and can be run by hand from the Actions tab.

1. `pipeline/build.py` collects the talks from each series' source (University events pages, researchseminars.org, Indico and group web pages) into `site/data/talks.json`. A source that fails keeps its last good copy.
2. `pipeline/calendars.py` writes the calendar files from that data.
3. The Action commits only when a talk has changed, and GitHub Pages republishes.

The list of series and their sources is in `pipeline/series.json`. Update its `term` dates at the start of each term.
