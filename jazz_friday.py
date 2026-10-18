#!/usr/bin/env python3
import argparse
import datetime as dt
import difflib
import html
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request

QOBUZ = "https://www.qobuz.com"
LOCALE = "fr-fr"            # Qobuz store locale
APPLE_COUNTRY = "fr"        # Apple Music storefront
WEEKS_ON_HOME = 4           # Weeks on front page
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


def fetch(url, tries=5, pause=3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept-Language": "fr-FR,fr;q=0.9"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # Network hiccups, 5xx
            last = e
            time.sleep(pause * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last}")


def text(s):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s))).strip()


# ---------------------------------------------------------------- Qobuz

def qobuz_curated():
    """Albums in the editorial “Les nouveautés Jazz” block of the jazz shop page."""
    page = fetch(f"{QOBUZ}/{LOCALE}/shop/jazz/download-streaming-albums")
    start = page.find('id="newAlbum"')
    if start < 0:
        raise RuntimeError("Qobuz page layout changed: 'newAlbum' block not found")
    end = page.find('id="newAlbum"', start + 1)
    block = page[start:end if end > 0 else start + 200000]
    stop = block.find("</ul>", block.find('class="filter-items"'))
    block = block[:stop] if stop > 0 else block

    albums = []
    for li in re.findall(r'<li class="album-container[^"]*">(.*?)</li>', block, re.S):
        m = re.search(r'href="(/[^"]*/album/[^"]+/([a-z0-9]+))"', li)
        t = re.search(r'class="album-title[^"]*"[^>]*>(.*?)</a>', li, re.S)
        a = re.search(r'class="artist-name[^"]*"[^>]*>(.*?)</a>', li, re.S)
        if not (m and t):
            continue
        albums.append({
            "id": m.group(2),
            "qobuz": QOBUZ + m.group(1),
            "title": text(t.group(1)),
            "artist": text(a.group(1)) if a else "",
            "hires": "smr-audio" in li,
        })
    if not albums:
        raise RuntimeError("Qobuz page layout changed: no albums parsed")
    return albums


def qobuz_details(album):
    page = fetch(album["qobuz"])
    out = {}
    for blob in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        if data.get("@type") == "Product":
            out["released"] = data.get("releaseDate")
            imgs = data.get("image") or []
            if imgs:
                out["cover"] = imgs[-1]
    for item in re.findall(r'<li class="album-meta__item">(.*?)</li>', page, re.S):
        line = text(item)
        if " chez " in line and line.startswith("Paru le"):
            out["label"] = line.split(" chez ", 1)[1]
            if not out.get("released"):
                d = re.search(r"(\d{2})/(\d{2})/(\d{4})", line)
                if d:
                    out["released"] = f"{d.group(3)}-{d.group(2)}-{d.group(1)}"
        elif line.startswith("Genre"):
            out["genre"] = line.split(":", 1)[1].strip()
        elif line.startswith("Artiste"):
            out["artists"] = line.split(":", 1)[1].strip()
    return out


# ---------------------------------------------------------- Apple Music

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\((deluxe|remaster|live|expanded|bonus)[^)]*\)", "", s)
    s = re.sub(r"\s*-\s*(ep|single)$", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def apple_lookup(artist, title, released=None):
    term = urllib.parse.quote(f"{artist} {title}")
    url = (f"https://itunes.apple.com/search?term={term}&entity=album"
           f"&country={APPLE_COUNTRY}&limit=10")
    try:
        results = json.loads(fetch(url)).get("results", [])
    except Exception:
        return None
    best, best_score = None, 0.0
    na, nt = norm(artist), norm(title)
    for r in results:
        rt = norm(r.get("collectionName", ""))
        ra = norm(r.get("artistName", ""))
        t_score = difflib.SequenceMatcher(None, nt, rt).ratio()
        if nt and (nt in rt or rt in nt):
            t_score = max(t_score, 0.9)
        a_score = difflib.SequenceMatcher(None, na, ra).ratio()
        if na and (set(na.split()) & set(ra.split())):
            a_score = max(a_score, 0.8)
        score = t_score * 0.6 + a_score * 0.4
        if released and r.get("releaseDate"):
            try:
                gap = abs((dt.date.fromisoformat(r["releaseDate"][:10])
                           - dt.date.fromisoformat(released)).days)
            except ValueError:
                gap = 0
            if gap > 180:      # Same name, different (older) record
                continue
            score += 0.1 if gap <= 14 else 0
        if (r.get("trackCount") or 0) >= 4:
            score += 0.05      # Prefer the album over a single of the same name
        if t_score >= 0.75 and a_score >= 0.6 and score > best_score:
            best, best_score = r, score
    if not best:
        return None
    return best.get("collectionViewUrl", "").split("?")[0] or None


# --------------------------------------------------------------- State

def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"albums": {}}


def atomic_write(path, data):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ---------------------------------------------------------------- Pages

def week_friday(date_str, fallback):
    """Friday that closes the Saturday-to-Friday week containing the date."""
    try:
        d = dt.date.fromisoformat(date_str)
    except (TypeError, ValueError):
        d = fallback
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def fmt_day(d):
    return f"{d.day} {d.strftime('%B %Y')}"


CSS = """
:root {
  --background-body: hsl(0 0% 93%);
  --background-card: hsl(275 5% 95% / 50%);
  --background-glass: hsl(275 10% 90% / 50%);
  --border-article: hsl(275 15% 70% / 50%);
  --border-glass: hsl(275 15% 85% / 50%);
  --colour-body: hsl(275 15% 15%);
  --colour-glass: hsl(275 5% 30%);
  --shadow-glass: hsl(275 15% 25% / 50%);
  --space-3xs: clamp(0.25rem, 0.2188rem + 0.1563vw, 0.3125rem);
  --space-2xs: clamp(0.5rem, 0.4688rem + 0.1563vw, 0.5625rem);
  --space-xs: clamp(0.75rem, 0.6875rem + 0.3125vw, 0.875rem);
  --space-s: clamp(1rem, 0.9375rem + 0.3125vw, 1.125rem);
  --space-m: clamp(1.5rem, 1.4063rem + 0.4688vw, 1.6875rem);
  --space-l: clamp(2rem, 1.875rem + 0.625vw, 2.25rem);
  --space-xl: clamp(3rem, 2.8125rem + 0.9375vw, 3.375rem);
}
@media (prefers-color-scheme: dark) {
  :root {
    --background-body: hsl(275 8% 12%);
    --background-card: hsl(275 5% 15% / 50%);
    --background-glass: hsl(275 5% 15% / 50%);
    --border-article: hsl(275 15% 70% / 50%);
    --border-glass: hsl(275 10% 95% / 50%);
    --colour-body: hsl(275 5% 80%);
    --colour-glass: hsl(275 5% 90%);
    --shadow-glass: hsl(275 15% 15% / 50%);
  }
}
* { box-sizing: border-box; }
body {
  background: var(--background-body);
  color: var(--colour-body);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
  margin: 0;
}
a { color: inherit; }
main { margin: 0 auto; max-width: 1100px; padding: var(--space-l) var(--space-s) var(--space-xl); }
header h1 { font-size: 1.75rem; line-height: 1.1; margin: 0 0 var(--space-3xs); }
header h1 a { text-decoration: none; }
header p { color: var(--colour-glass); font-size: 0.85rem; margin: 0; }
nav { display: flex; flex-wrap: wrap; gap: var(--space-3xs); margin: var(--space-s) 0 0; }
nav a {
  background: var(--background-glass);
  border: 1px solid var(--border-glass);
  border-radius: 999px;
  color: var(--colour-glass);
  font-size: 0.85rem;
  padding: 3px var(--space-2xs);
  text-decoration: none;
}
nav a:hover { border-color: var(--border-article); color: var(--colour-body); }
section { margin-top: var(--space-l); }
h2 { align-items: baseline; display: flex; font-size: 1.15rem; gap: var(--space-2xs); line-height: 1.2; margin: 0 0 var(--space-xs); }
h2 small { color: var(--colour-glass); font-size: 0.85rem; font-weight: 400; }
.grid { display: grid; gap: var(--space-xs); grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); }
.card {
  background: var(--background-card);
  border: 1px solid var(--border-article);
  border-radius: 10px;
  box-shadow: 0 1px 3px var(--shadow-glass);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.card img { aspect-ratio: 1; background: var(--background-glass); display: block; object-fit: cover; width: 100%; }
.body { display: flex; flex: 1; flex-direction: column; gap: 3px; padding: var(--space-2xs) var(--space-xs) var(--space-xs); }
.artist { font-weight: 600; }
.title { font-style: italic; }
.meta { color: var(--colour-glass); font-size: 0.8rem; }
.links { display: flex; flex-wrap: wrap; gap: var(--space-3xs); margin-top: auto; padding-top: var(--space-2xs); }
.links a {
  border: 1px solid var(--border-article);
  border-radius: 6px;
  font-size: 0.8rem;
  padding: 4px var(--space-2xs);
  text-decoration: none;
}
.links a.am { background: var(--colour-body); border-color: var(--colour-body); color: var(--background-body); }
.links a.bc { border-style: dashed; }
.weeks { list-style: none; margin: var(--space-s) 0 0; padding: 0; }
.weeks li { border-bottom: 1px solid var(--border-article); display: flex; justify-content: space-between; padding: var(--space-2xs) 0; }
.weeks a { text-decoration: none; }
.weeks span { color: var(--colour-glass); }
.pager { display: flex; justify-content: space-between; margin-top: var(--space-l); }
.empty { color: var(--colour-glass); }
@media (max-width: 520px) {
  .body { padding: var(--space-2xs); }
  .grid { gap: var(--space-2xs); grid-template-columns: 1fr 1fr; }
  .links a { padding: 3px 6px; }
}
"""


def card(a):
    cover = a.get("cover") or ""
    meta = " · ".join(x for x in [a.get("label"), a.get("genre"),
                                  "Hi-Res" if a.get("hires") else None] if x)
    if a.get("apple"):
        am = f'<a class="am" href="{html.escape(a["apple"])}">Apple Music</a>'
    else:
        q = urllib.parse.quote(f'{a["artist"]} {a["title"]}')
        am = (f'<a class="bc" href="https://bandcamp.com/search?q={q}&amp;item_type=a">'
              f'Not on Apple Music · Bandcamp</a>')
    return (f'<article class="card">'
            f'<img loading="lazy" src="{html.escape(cover)}" alt="">'
            f'<div class="body"><span class="artist">{html.escape(a["artist"])}</span>'
            f'<span class="title">{html.escape(a["title"])}</span>'
            f'<span class="meta">{html.escape(meta)}</span>'
            f'<div class="links">{am}<a href="{html.escape(a["qobuz"])}">Qobuz</a></div>'
            f'</div></article>')


def grid(items, key):
    return f'<div class="grid">{"".join(card(a) for a in sorted(items, key=key))}</div>'


def albums(n):
    return f"{n} album" + ("" if n == 1 else "s")


def by_artist(a):
    return (a["artist"].lower(), a["title"].lower())


def page(title, root, body, nav=""):
    updated = dt.datetime.now().strftime("%-d %B %Y, %H:%M")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#ededed" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1f1c21" media="(prefers-color-scheme: dark)">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><main>
<header><h1><a href="{root}index.html">Jazz Friday</a></h1>
<p>New jazz releases picked by Qobuz’s editors · Updated {updated}</p></header>
<nav>{nav}</nav>
{body}
</main></body></html>
"""


def build_site(state, today, out_dir):
    weeks, upcoming = {}, []
    for a in state["albums"].values():
        if (a.get("released") or "") > today.isoformat():
            upcoming.append(a)
            continue
        first = dt.date.fromisoformat(a["first_seen"])
        weeks.setdefault(week_friday(a.get("released"), first), []).append(a)
    fridays = sorted(weeks, reverse=True)
    files = {}

    def week_section(w):
        return (f'<section id="w{w.isoformat()}"><h2>Friday {fmt_day(w)}'
                f'<small>{albums(len(weeks[w]))}</small></h2>{grid(weeks[w], by_artist)}</section>')

    # Front page: upcoming releases and the latest weeks.
    body, nav = [], []
    if upcoming:
        body.append(f'<section id="upcoming"><h2>Coming up<small>{albums(len(upcoming))}</small></h2>'
                    f'{grid(upcoming, lambda a: (a.get("released") or "", by_artist(a)))}</section>')
        nav.append('<a href="#upcoming">Coming up</a>')
    for w in fridays[:WEEKS_ON_HOME]:
        body.append(week_section(w))
        nav.append(f'<a href="#w{w.isoformat()}">{fmt_day(w)}</a>')
    nav.append('<a href="archive/index.html">Archive</a>')
    files["index.html"] = page("Jazz Friday", "", "".join(body) or '<p class="empty">Nothing yet.</p>',
                               "".join(nav))

    # One page per week.
    for i, w in enumerate(fridays):
        newer = fridays[i - 1] if i > 0 else None
        older = fridays[i + 1] if i + 1 < len(fridays) else None
        pager = '<div class="pager">'
        pager += f'<a href="{older.isoformat()}.html">← {fmt_day(older)}</a>' if older else "<span></span>"
        pager += f'<a href="{newer.isoformat()}.html">{fmt_day(newer)} →</a>' if newer else "<span></span>"
        pager += "</div>"
        files[f"archive/{w.isoformat()}.html"] = page(
            f"Jazz Friday · {fmt_day(w)}", "../", week_section(w) + pager,
            '<a href="../index.html">Latest</a><a href="index.html">Archive</a>')

    # Archive index.
    rows = "".join(f'<li><a href="{w.isoformat()}.html">Friday {fmt_day(w)}</a>'
                   f'<span>{albums(len(weeks[w]))}</span></li>' for w in fridays)
    files["archive/index.html"] = page("Jazz Friday · Archive", "../",
                                       f'<section><h2>Archive</h2><ul class="weeks">{rows}</ul></section>',
                                       '<a href="../index.html">Latest</a>')

    for rel, content in files.items():
        atomic_write(os.path.join(out_dir, rel), content)
    return len(fridays)


# ---------------------------------------------------------------- Main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True, help="Web root to write the pages into")
    ap.add_argument("--state", required=True, help="JSON file that remembers every album")
    args = ap.parse_args()

    today = dt.date.today()
    state = load_state(args.state)
    known = state["albums"]

    new = 0
    for a in qobuz_curated():
        if a["id"] in known:
            continue
        try:
            a.update(qobuz_details(a))
        except Exception as e:
            print(f"warn: details for {a['qobuz']}: {e}", file=sys.stderr)
        a["apple"] = apple_lookup(a["artist"], a["title"], a.get("released"))
        if not a["apple"] and a.get("artists") and a["artists"] != a["artist"]:
            a["apple"] = apple_lookup(a["artists"], a["title"], a.get("released"))
        a["first_seen"] = today.isoformat()
        known[a["id"]] = a
        new += 1
        time.sleep(3)  # Stay well under Apple’s search rate limit

    atomic_write(args.state, json.dumps(state, ensure_ascii=False, indent=1))
    weeks = build_site(state, today, args.out_dir)
    print(f"{new} new album(s); {len(known)} albums across {weeks} weeks")


if __name__ == "__main__":
    main()
