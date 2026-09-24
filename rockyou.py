#!/usr/bin/env python3
"""
Rockyou — Roku free live-TV playlist + EPG generator (from Matt Huisman's feed).

Source : https://github.com/matthuisman/i.mjh.nz  Roku/.channels.json.gz
Streams: https://jmp2.uk/rok-<id>.m3u8   (Matt's resolver mints the real stream)

Why this exists: the channels JSON is a per-scrape SNAPSHOT. When Roku hands
Matt a partial lineup on a given run, channels vanish from that snapshot and
reappear days/weeks later (e.g. "Stingray Hooked" gone 9/23, still live on Roku).
A playlist built straight from one snapshot inherits those gaps.

Fix: a ROLLING MERGE. We keep a state file of every channel ever seen with a
last-seen date, and carry a channel through transient absences until it has
been gone for GRACE_DAYS. So the guide stops losing channels.

Outputs (committed to the repo, read by TiviMate):
  roku.m3u          flat playlist, jmp2.uk stream URLs, consolidated groups
  roku_epg.xml      XMLTV built from each channel's `programs`
  rockyou_state.json.gz   rolling state (do not delete)
"""

import sys
import gzip
import json
import time
import collections
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SOURCE_URL = "https://github.com/matthuisman/i.mjh.nz/raw/refs/heads/master/Roku/.channels.json.gz"
STREAM_TMPL = "https://jmp2.uk/rok-{id}.m3u8"

# Point this at YOUR repo's raw path so TiviMate pulls the EPG we generate.
EPG_RAW_URL = "https://raw.githubusercontent.com/s-digweed/Rockyou/main/roku_epg.xml"

STATE_FILE = "rockyou_state.json.gz"
M3U_FILE   = "roku.m3u"
EPG_FILE   = "roku_epg.xml"

# Keep a channel that has dropped out of the feed this long after it was last
# seen, then let it go as genuinely dead. 50 days rides out even Roku's
# month-long benching of a channel before treating it as truly removed.
GRACE_DAYS = 50

# Default duration for a channel's LAST programme (Roku blocks are ~4h).
LAST_PROG_SECONDS = 4 * 3600

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
REQ_TIMEOUT = 30

# Consolidate Roku's ~90 granular genre tags into a tidy set of groups.
# (Ported from the Buddy generator so groups match what you already use.)
GROUP_MAP = {
    # -> Auto & Motorsports
    "Auto": "Auto & Motorsports",
    "Auto Racing": "Auto & Motorsports",
    # -> Comedy
    "Romantic Comedy": "Comedy",
    # -> Documentary
    "Nature": "Documentary",
    # -> Faith & Family
    "Faith": "Faith & Family",
    "Family": "Faith & Family",
    "Religious": "Faith & Family",
    # -> Gaming & Tech
    "Computers": "Gaming & Tech",
    "Esports": "Gaming & Tech",
    "Gaming": "Gaming & Tech",
    # -> Health
    "Medical": "Health",
    # -> Movies
    "Adventure": "Movies",
    "Fantasy": "Movies",
    "Horror": "Movies",
    "Science Fiction": "Movies",
    "Suspense": "Movies",
    "Thriller": "Movies",
    # -> News
    "Newsmagazine": "News",
    "Politics": "News",
    "Special": "News",
    # -> Sports
    "Action": "Sports",
    "Action Sports": "Sports",
    "Baseball": "Sports",
    "Basketball": "Sports",
    "Bicycle": "Sports",
    "Billiards": "Sports",
    "Bmx Racing": "Sports",
    "Boat Racing": "Sports",
    "Boxing": "Sports",
    "Bullfighting": "Sports",
    "Cycling": "Sports",
    "Drag Racing": "Sports",
    "Fishing": "Sports",
    "Football": "Sports",
    "Golf": "Sports",
    "Hockey": "Sports",
    "Hunting": "Sports",
    "Judo": "Sports",
    "Karate": "Sports",
    "Martial Arts": "Sports",
    "Mixed Martial Arts": "Sports",
    "Motorcycle": "Sports",
    "Motorcycle Racing": "Sports",
    "Motorsports": "Sports",
    "Olympics": "Sports",
    "Outdoors": "Sports",
    "Rodeo": "Sports",
    "Rugby": "Sports",
    "Skateboarding": "Sports",
    "Snowboarding": "Sports",
    "Soccer": "Sports",
    "Sports Talk": "Sports",
    "Surfing": "Sports",
    "Tennis": "Sports",
    "Volleyball": "Sports",
    "Western": "Sports",
    "Wrestling": "Sports",
    # -> TV & Entertainment
    "Comedy Drama": "TV & Entertainment",
    "Drama": "TV & Entertainment",
    "Entertainment": "TV & Entertainment",
    "History": "TV & Entertainment",
    "Reality": "TV & Entertainment",
    "Sitcom": "TV & Entertainment",
    "Soap": "TV & Entertainment",
    "Talk": "TV & Entertainment",
}

# Per-channel group overrides, keyed by Roku channel id (stable across renames).
# These win over GROUP_MAP, and are how we retire one-channel categories by
# moving their sole member elsewhere.
CHANNEL_OVERRIDES = {
    "98cafb7f4b01848a967fda4bd2225bd7": "Sports",           # Unbeaten Sports (was 3X3 Basketball)
    "3b1bd759566ea4c0fe9d7d6eb354d23d": "Sports",           # Triton Poker (was Card Games)
    "ac0caef9123a35df3b884b139340a597": "Sports",           # CW Presents WWE NXT (was Pro Wrestling)
    "9aff620ece2457a3a7572886ecae0495": "Music",            # Super Simple Songs (was Children-Music)
    "ada4254a1dd65c138f62e7c45affb45c": "Home Improvement", # GARDEN with Monty Don (was House/Garden)
    "7e9fe2b4c7ba5869af6d60adbf4b7c86": "Home Improvement", # This Old House (was Educational)
    "c7825e03df4a5bf4bdc87d65b7e3cdbb": "Home Improvement", # This Old House Classic (was Educational)
    "ccfcc3307fa908684a95bf9643b1b935": "Home Improvement", # This Old House Shorts (was Educational)
    "4d62b7d4fc91fbc2b528eb10857d7ab8": "Documentary",      # Space Live (was Educational)
    "0141916713c53d074650f14e2c9ff61e": "Crime",            # Britbox Mysteries (was Crime Drama)
    "586bd3d19f6e4b7a5cf3458722984b82": "Entertainment",    # Court TV Legendary Trials (was Law)
    "091bf303c7fd2bc2ec6683b1dbbadae1": "Entertainment",    # Storage Wars by A&E (was Auction)
}

# Final display-name rename applied to every group (catches both mapped genres
# and groups carried in on seeded/retained channels).
GROUP_RENAME = {
    "TV & Entertainment": "Entertainment",
}


def now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def fetch_channels():
    """Download and parse Matt's Roku channels JSON (gzipped)."""
    r = requests.get(SOURCE_URL, headers={"User-Agent": UA}, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    raw = gzip.decompress(r.content)
    data = json.loads(raw)
    chans = data.get("channels", {})
    if not chans:
        raise RuntimeError("source returned no channels")
    return chans


# ---------------------------------------------------------------------------
# Rolling merge — the disappearing-channel fix
# ---------------------------------------------------------------------------

def load_state():
    try:
        with gzip.open(STATE_FILE, "rt", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("No prior state (first run).")
        return {}
    except Exception as e:
        print(f"State load warning: {e}")
        return {}


def save_state(state):
    try:
        with gzip.open(STATE_FILE, "wt", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"State save warning: {e}")


def merge(current, state):
    """Union this run's channels with retained state.

    current: {id: channel dict from the feed}
    state:   {id: channel dict + 'last_seen' + 'missing_runs'} from prior runs
    Returns (merged {id: channel}, stats).
    """
    now = now_ts()
    grace = GRACE_DAYS * 86400
    merged = {}
    seen_now = set(current)

    # 1) everything in this run: refresh data, stamp last_seen
    for cid, ch in current.items():
        rec = dict(ch)
        rec["last_seen"] = now
        rec["missing_runs"] = 0
        merged[cid] = rec

    # 2) channels only in prior state: keep within the grace window
    retained, dropped = [], []
    for cid, ch in state.items():
        if cid in seen_now:
            continue
        last = ch.get("last_seen", 0)
        if now - last <= grace:
            rec = dict(ch)
            rec["missing_runs"] = ch.get("missing_runs", 0) + 1
            merged[cid] = rec                    # carry it forward
            retained.append((ch.get("name", cid), int((now - last) / 86400)))
        else:
            dropped.append((ch.get("name", cid), int((now - last) / 86400)))

    stats = {
        "in_feed": len(current),
        "retained": retained,
        "dropped": dropped,
        "total": len(merged),
    }
    return merged, stats


# ---------------------------------------------------------------------------
# Output builders
# ---------------------------------------------------------------------------

def group_of(ch, cid=None):
    if cid and cid in CHANNEL_OVERRIDES:
        g = CHANNEL_OVERRIDES[cid]
    else:
        raw = (ch.get("groups") or ["Other"])[0] if ch.get("groups") else "Other"
        g = GROUP_MAP.get(raw, raw)
    return GROUP_RENAME.get(g, g)


def m3u_escape(s):
    return str(s).replace('"', "'").replace("\n", " ").strip()


def build_m3u(merged):
    lines = [f'#EXTM3U url-tvg="{EPG_RAW_URL}"']
    # group, then name — matches the ordering you already have
    ordered = sorted(
        merged.items(),
        key=lambda kv: (group_of(kv[1], kv[0]).lower(), (kv[1].get("name") or "").lower()),
    )
    for cid, ch in ordered:
        name = m3u_escape(ch.get("name") or cid)
        chno = ch.get("chno")
        chno = str(chno) if chno is not None and str(chno).isdigit() else ""
        logo = ch.get("logo") or ""
        grp = m3u_escape(group_of(ch, cid))
        lines.append(
            f'#EXTINF:-1 channel-id="{cid}" tvg-id="{cid}" tvg-chno="{chno}" '
            f'tvg-name="{name}" tvg-logo="{logo}" group-title="{grp}",{name}'
        )
        lines.append(STREAM_TMPL.format(id=cid))
    return "\n".join(lines) + "\n"


def xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def xt(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def build_epg(merged):
    now = now_ts()
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>"]
    # channel elements
    for cid, ch in merged.items():
        name = xml_escape(ch.get("name") or cid)
        logo = ch.get("logo") or ""
        el = f'<channel id="{cid}"><display-name>{name}</display-name>'
        if logo:
            el += f'<icon src="{xml_escape(logo)}" />'
        el += "</channel>"
        out.append(el)
    # programmes — build stops from the next start; prune anything already ended
    for cid, ch in merged.items():
        progs = ch.get("programs") or []
        if not progs:
            continue
        progs = sorted(progs, key=lambda p: p[0])
        for i, p in enumerate(progs):
            start = int(p[0])
            title = p[1] if len(p) > 1 else (ch.get("name") or "")
            stop = int(progs[i + 1][0]) if i + 1 < len(progs) else start + LAST_PROG_SECONDS
            if stop <= now:
                continue                      # drop finished programmes
            out.append(
                f'<programme start="{xt(start)}" stop="{xt(stop)}" channel="{cid}">'
                f"<title>{xml_escape(title)}</title></programme>"
            )
    out.append("</tv>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(state_in=None, current_override=None):
    """Core pipeline. Params let tests inject data; production uses neither."""
    current = current_override if current_override is not None else fetch_channels()
    print(f"Feed: {len(current)} channels")

    state = state_in if state_in is not None else load_state()
    merged, stats = merge(current, state)

    print(f"Retained through gaps: {len(stats['retained'])}")
    for name, days in sorted(stats["retained"], key=lambda x: -x[1])[:25]:
        print(f"  + kept {name}  (missing {days}d)")
    if stats["dropped"]:
        print(f"Dropped (dead > {GRACE_DAYS}d): {len(stats['dropped'])}")
        for name, days in stats["dropped"]:
            print(f"  - dropped {name}  (missing {days}d)")

    m3u = build_m3u(merged)
    epg = build_epg(merged)

    with open(M3U_FILE, "w", encoding="utf-8") as f:
        f.write(m3u)
    with open(EPG_FILE, "w", encoding="utf-8") as f:
        f.write(epg)
    save_state(merged)

    n_prog = epg.count("<programme ")
    print(f"Wrote {M3U_FILE} ({len(merged)} channels) and {EPG_FILE} ({n_prog} programmes)")
    return merged, stats


def main():
    try:
        run()
    except Exception as e:
        print(f"ERROR: {e}")
        print("Leaving previous files untouched.")
        sys.exit(1)


if __name__ == "__main__":
    main()
