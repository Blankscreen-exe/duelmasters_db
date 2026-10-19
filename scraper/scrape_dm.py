"""
Scrape https://db.duelmasters.us/ into a local SQLite database + image folder.

Standard library only (no pip installs needed).

Usage (from the project root):
    python scraper/scrape_dm.py                 # full scrape (resumable)
    python scraper/scrape_dm.py --limit 20      # quick test on 20 cards
    python scraper/scrape_dm.py --skip-images   # data only
    python scraper/scrape_dm.py --videos        # also download card videos (.mp4)
    python scraper/scrape_dm.py --refresh       # re-fetch card detail pages

Output:
    data/duelmasters.db      SQLite database
    data/images/<id>.webp    card images
    data/icons/...           civilization / rarity / set / keyword icons + site banner
    data/videos/<id>.mp4     card videos (only with --videos)
    data/cards.json          denormalized JSON export (handy for game engines)
"""

import argparse
import json
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path

BASE_URL = "https://db.duelmasters.us"
IMG_URL = "https://img.duelmasters.us/{id}.webp"
USER_AGENT = "Mozilla/5.0 (personal card-db mirror; low request rate)"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "duelmasters.db"
IMG_DIR = DATA_DIR / "images"
VIDEO_DIR = DATA_DIR / "videos"
JSON_PATH = DATA_DIR / "cards.json"

# Fix typos in the source data's card types ("Creatue", "creature", ...)
TYPE_FIXES = {"creatue": "creature"}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

_throttle_lock = threading.Lock()
_last_request = [0.0]


def fetch(url, delay, retries=4, binary=False):
    """GET a URL with a global minimum delay between requests and retries."""
    for attempt in range(retries + 1):
        with _throttle_lock:
            wait = _last_request[0] + delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_request[0] = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = resp.read()
                return body if binary else body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries or e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries:
                raise
        time.sleep(2 ** attempt * 2)


# --------------------------------------------------------------------------
# Minimal DOM built on html.parser
# --------------------------------------------------------------------------

VOID_TAGS = {"img", "br", "input", "meta", "link", "hr", "source", "wbr"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.children, self.parent = tag, dict(attrs), [], parent

    def iter(self):
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.iter()

    def find_all(self, tag=None, **attrs):
        return [n for n in self.iter()
                if (tag is None or n.tag == tag)
                and all(n.attrs.get(k) == v for k, v in attrs.items())]

    def find(self, tag=None, **attrs):
        return next(iter(self.find_all(tag, **attrs)), None)

    def text(self):
        parts = []
        for c in self.children:
            parts.append(c.text() if isinstance(c, Node) else c)
        return "".join(parts)

    def clean_text(self):
        return re.sub(r"\s+", " ", self.text()).strip()


class DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", [], None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in VOID_TAGS:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_html(html):
    b = DomBuilder()
    b.feed(html)
    b.close()
    return b.root


def icon_name(src):
    """'/icons/rarities/rarity-vr.png' -> 'rarity-vr'"""
    return Path(urllib.parse.urlparse(src or "").path).stem


def to_int(s):
    m = re.search(r"-?\d+", s or "")
    return int(m.group()) if m else None


# --------------------------------------------------------------------------
# Page parsers
# --------------------------------------------------------------------------

def parse_search_results(html):
    """Return list of card dicts from a /search results page."""
    dom = parse_html(html)
    table = dom.find("table", id="results-table")
    if table is None:
        return []
    cards = []
    for tr in table.find_all("tr"):
        if "data-id" not in tr.attrs:
            continue
        tds = [c for c in tr.children if isinstance(c, Node) and c.tag == "td"]
        if len(tds) < 10:
            continue
        civ_imgs = tds[2].find_all("img")
        rarity_img = tds[7].find("img")
        set_img = tds[9].find("img")
        raw_type = tds[4].clean_text()
        cards.append({
            "id": tr.attrs["data-id"],
            "name": tds[1].clean_text(),
            "civilizations": [i.attrs.get("title", "") for i in civ_imgs],
            "cost": to_int(tds[3].clean_text()),
            "card_type_raw": raw_type,
            "card_type": normalize_type(raw_type),
            "races": [r.strip() for r in tds[5].clean_text().split("/") if r.strip()],
            "power": to_int(tds[6].clean_text()),
            "rarity": rarity_img.attrs.get("title") if rarity_img else None,
            "rarity_code": icon_name(rarity_img.attrs.get("src")).replace("rarity-", "") if rarity_img else None,
            "collector_number": tds[8].clean_text() or None,
            "set_code": set_img.attrs.get("title") if set_img else None,
            "set_icon": icon_name(set_img.attrs.get("src")) if set_img else None,
        })
    return cards


def parse_filter_options(html, select_id):
    """Return [(value, label, group)] for a <select> on the search page."""
    dom = parse_html(html)
    sel = dom.find("select", id=select_id)
    out = []
    if sel is None:
        return out
    for opt in sel.find_all("option"):
        value = opt.attrs.get("value", "")
        if not value:
            continue
        group = opt.parent.attrs.get("label") if opt.parent.tag == "optgroup" else None
        out.append((value, opt.clean_text(), group))
    return out


def normalize_type(raw):
    t = raw.strip().lower()
    for bad, good in TYPE_FIXES.items():
        t = t.replace(bad, good)
    return " ".join(w.capitalize() for w in t.split())


def parse_cardview(html):
    """Return detail dict from a /cardview/<id> page."""
    dom = parse_html(html)
    info = dom.find("div", id="card-info-container")
    if info is None:
        return None
    fields = {}
    wiki_url = None
    for p in info.find_all("p"):
        b = p.find("b")
        if b is None:
            continue
        label = b.clean_text().rstrip(":").strip()
        b_text = b.clean_text()
        value = p.clean_text()[len(b_text):].strip()
        fields[label] = value
        a = p.find("a")
        if label == "Name" and a is not None:
            wiki_url = a.attrs.get("href")

    abilities = []
    ul = info.find("ul", id="rules-text")
    if ul is not None:
        for li in ul.find_all("li"):
            img = li.find("img")
            reminder = " ".join(i.clean_text() for i in li.find_all("i")) or None
            abilities.append({
                "text": li.clean_text(),
                "reminder": reminder,
                "keyword_icon": icon_name(img.attrs.get("src")) if img is not None else None,
            })

    video = dom.find("video")
    return {
        "fields": fields,
        "wiki_url": wiki_url.replace(" ", "_") if wiki_url else None,
        "abilities": abilities,
        "video_url": video.attrs.get("src") if video is not None else None,
    }


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS sets (
    code        TEXT PRIMARY KEY,      -- e.g. 'DM-01', 'PROMO'
    icon        TEXT
);

CREATE TABLE IF NOT EXISTS cards (
    id                TEXT PRIMARY KEY,   -- site id, e.g. '0079' (also image filename)
    name              TEXT NOT NULL,
    card_type         TEXT,               -- normalized: Creature, Spell, Evolution Creature, ...
    card_type_raw     TEXT,               -- as shown on the site
    cost              INTEGER,
    power             INTEGER,            -- NULL for spells/cross gear/castles
    power_text        TEXT,               -- raw power string from detail page (e.g. '6000+')
    civilizations     TEXT,               -- '/'-joined, e.g. 'Water/Darkness'
    races             TEXT,               -- '/'-joined
    rarity            TEXT,               -- 'Very Rare'
    rarity_code       TEXT,               -- 'vr'
    collector_number  TEXT,               -- '69/110'
    set_code          TEXT REFERENCES sets(code),
    rules_text        TEXT,               -- all ability lines joined by newline
    wiki_url          TEXT,
    video_url         TEXT,
    image_path        TEXT,               -- relative to data/, e.g. 'images/0079.webp'
    video_path        TEXT,
    extra_fields      TEXT,               -- JSON of any detail fields not mapped above
    detail_fetched    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS card_civilizations (
    card_id      TEXT REFERENCES cards(id) ON DELETE CASCADE,
    civilization TEXT,
    PRIMARY KEY (card_id, civilization)
);

CREATE TABLE IF NOT EXISTS card_races (
    card_id TEXT REFERENCES cards(id) ON DELETE CASCADE,
    race    TEXT,
    PRIMARY KEY (card_id, race)
);

CREATE TABLE IF NOT EXISTS card_abilities (
    card_id      TEXT REFERENCES cards(id) ON DELETE CASCADE,
    position     INTEGER,
    text         TEXT,
    reminder     TEXT,     -- italic reminder text, if any
    keyword_icon TEXT,     -- e.g. 'blocker', 'shield_trigger' (from the bullet icon)
    PRIMARY KEY (card_id, position)
);

CREATE TABLE IF NOT EXISTS keywords (
    code  TEXT PRIMARY KEY,    -- e.g. 'shield_trigger'
    label TEXT
);

CREATE TABLE IF NOT EXISTS card_keywords (
    card_id TEXT REFERENCES cards(id) ON DELETE CASCADE,
    keyword TEXT REFERENCES keywords(code),
    PRIMARY KEY (card_id, keyword)
);

CREATE TABLE IF NOT EXISTS categories (
    code       TEXT PRIMARY KEY,   -- e.g. 'removal_destroy_creature'
    label      TEXT,
    group_name TEXT
);

CREATE TABLE IF NOT EXISTS card_categories (
    card_id  TEXT REFERENCES cards(id) ON DELETE CASCADE,
    category TEXT REFERENCES categories(code),
    PRIMARY KEY (card_id, category)
);

CREATE TABLE IF NOT EXISTS icons (
    kind  TEXT,    -- 'civs', 'rarities', 'sets', 'keywords', 'site'
    code  TEXT,    -- matches: lower(civilization), cards.rarity_code, sets.icon, card_abilities.keyword_icon
    path  TEXT,    -- relative to data/, e.g. 'icons/civs/fire.webp'
    PRIMARY KEY (kind, code)
);

CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name);
CREATE INDEX IF NOT EXISTS idx_cards_set  ON cards(set_code);
CREATE INDEX IF NOT EXISTS idx_civ        ON card_civilizations(civilization);
CREATE INDEX IF NOT EXISTS idx_race       ON card_races(race);
CREATE INDEX IF NOT EXISTS idx_kw         ON card_keywords(keyword);
CREATE INDEX IF NOT EXISTS idx_cat        ON card_categories(category);
"""


def open_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    return db


def upsert_card_base(db, c):
    if c["set_code"]:
        db.execute("INSERT OR IGNORE INTO sets(code, icon) VALUES (?, ?)",
                   (c["set_code"], c["set_icon"]))
    db.execute("""
        INSERT INTO cards (id, name, card_type, card_type_raw, cost, power, civilizations,
                           races, rarity, rarity_code, collector_number, set_code)
        VALUES (:id, :name, :card_type, :card_type_raw, :cost, :power, :civs,
                :races_s, :rarity, :rarity_code, :collector_number, :set_code)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, card_type=excluded.card_type,
            card_type_raw=excluded.card_type_raw, cost=excluded.cost, power=excluded.power,
            civilizations=excluded.civilizations, races=excluded.races,
            rarity=excluded.rarity, rarity_code=excluded.rarity_code,
            collector_number=excluded.collector_number, set_code=excluded.set_code
    """, {**c, "civs": "/".join(c["civilizations"]), "races_s": "/".join(c["races"])})
    db.execute("DELETE FROM card_civilizations WHERE card_id=?", (c["id"],))
    db.executemany("INSERT OR IGNORE INTO card_civilizations VALUES (?, ?)",
                   [(c["id"], v) for v in c["civilizations"]])
    db.execute("DELETE FROM card_races WHERE card_id=?", (c["id"],))
    db.executemany("INSERT OR IGNORE INTO card_races VALUES (?, ?)",
                   [(c["id"], v) for v in c["races"]])


MAPPED_FIELDS = {"Name", "Civilization", "Card Type", "Cost", "Race", "Power", "Card Rules Text"}


def save_detail(db, card_id, d):
    f = d["fields"]
    extra = {k: v for k, v in f.items() if k not in MAPPED_FIELDS}
    db.execute("""
        UPDATE cards SET power_text=?, rules_text=?, wiki_url=?, video_url=?,
                         extra_fields=?, detail_fetched=1
        WHERE id=?
    """, (f.get("Power") or None,
          "\n".join(a["text"] for a in d["abilities"]) or None,
          d["wiki_url"], d["video_url"],
          json.dumps(extra, ensure_ascii=False) if extra else None,
          card_id))
    db.execute("DELETE FROM card_abilities WHERE card_id=?", (card_id,))
    db.executemany("INSERT INTO card_abilities VALUES (?, ?, ?, ?, ?)",
                   [(card_id, i, a["text"], a["reminder"], a["keyword_icon"])
                    for i, a in enumerate(d["abilities"])])


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def step_card_list(db, args):
    print("[1/5] Fetching full card list ...", flush=True)
    html = fetch(f"{BASE_URL}/search", args.delay)
    cards = parse_search_results(html)
    if not cards:
        sys.exit("No cards parsed from /search - the site layout may have changed.")
    if args.limit:
        cards = cards[:args.limit]
    for c in cards:
        upsert_card_base(db, c)
    db.commit()
    print(f"      {len(cards)} cards saved.", flush=True)
    return html, {c["id"] for c in cards}


def step_tags(db, args, search_html, card_ids):
    print("[2/5] Fetching keyword & category tags ...", flush=True)
    jobs = []
    for value, label, _ in parse_filter_options(search_html, "keyword_filter"):
        db.execute("INSERT OR REPLACE INTO keywords VALUES (?, ?)", (value, label))
        jobs.append(("keywords", value))
    for value, label, group in parse_filter_options(search_html, "category_filter"):
        db.execute("INSERT OR REPLACE INTO categories VALUES (?, ?, ?)", (value, label, group))
        jobs.append(("categories", value))
    db.commit()

    def run(job):
        param, value = job
        url = f"{BASE_URL}/search?{urllib.parse.urlencode({param: value})}"
        return job, [c["id"] for c in parse_search_results(fetch(url, args.delay))]

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for fut in as_completed([pool.submit(run, j) for j in jobs]):
            (param, value), ids = fut.result()
            table, col = ("card_keywords", "keyword") if param == "keywords" else ("card_categories", "category")
            db.execute(f"DELETE FROM {table} WHERE {col}=?", (value,))
            db.executemany(f"INSERT OR IGNORE INTO {table} VALUES (?, ?)",
                           [(i, value) for i in ids if i in card_ids])
            db.commit()
            done += 1
            print(f"      {done}/{len(jobs)} {param}={value}: {len(ids)} cards", flush=True)


def step_details(db, args, card_ids):
    if args.refresh:
        todo = sorted(card_ids)
    else:
        fetched = {r[0] for r in db.execute("SELECT id FROM cards WHERE detail_fetched=1")}
        todo = sorted(card_ids - fetched)
    print(f"[3/5] Fetching card detail pages ({len(todo)} to go) ...", flush=True)

    def run(cid):
        html = fetch(f"{BASE_URL}/cardview/{cid}", args.delay)
        return cid, parse_cardview(html) if html else None

    failed = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, cid) for cid in todo]
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                cid, detail = fut.result()
            except Exception as e:  # keep going; rerun the script to retry
                failed.append(str(e))
                continue
            if detail is None:
                failed.append(cid)
                continue
            save_detail(db, cid, detail)
            if n % 50 == 0 or n == len(todo):
                db.commit()
                print(f"      {n}/{len(todo)}", flush=True)
    db.commit()
    if failed:
        print(f"      {len(failed)} failed (rerun the script to retry): {failed[:10]}", flush=True)


def download_files(db, args, label, rows, folder, column):
    folder.mkdir(parents=True, exist_ok=True)
    todo = [(cid, url) for cid, url in rows if url]
    print(f"{label} ({len(todo)} cards) ...", flush=True)

    def run(item):
        cid, url = item
        ext = Path(urllib.parse.urlparse(url).path).suffix
        dest = folder / f"{cid}{ext}"
        if dest.exists() and dest.stat().st_size > 0:
            return cid, dest, False
        data = fetch(url, args.delay, binary=True)
        if not data:
            return cid, None, False
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return cid, dest, True

    new = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, item) for item in todo]
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                cid, dest, downloaded = fut.result()
            except Exception:
                failed += 1
                continue
            if dest is None:
                failed += 1
                continue
            new += downloaded
            db.execute(f"UPDATE cards SET {column}=? WHERE id=?",
                       (dest.relative_to(DATA_DIR).as_posix(), cid))
            if n % 100 == 0 or n == len(todo):
                db.commit()
                print(f"      {n}/{len(todo)} ({new} new)", flush=True)
    db.commit()
    if failed:
        print(f"      {failed} failed (rerun the script to retry)", flush=True)


SITE_ASSETS = ["dmdb-banner.webp", "favicon.ico"]


def icon_code(kind, path):
    stem = Path(path).stem
    return stem.replace("rarity-", "") if kind == "rarities" else stem


def step_icons(db, args, search_html):
    """Download civ/rarity/set/keyword icons (+ site banner) into data/icons/."""
    paths = set()
    for img in parse_html(search_html).find_all("img"):
        p = urllib.parse.urlparse(img.attrs.get("src", "")).path.lstrip("/")
        if p.startswith("icons/"):
            paths.add(p)
    for (kw,) in db.execute("SELECT DISTINCT keyword_icon FROM card_abilities WHERE keyword_icon IS NOT NULL"):
        paths.add(f"icons/keywords/{kw}.webp")
    print(f"[4a]  Downloading icons ({len(paths) + len(SITE_ASSETS)}) ...", flush=True)

    jobs = [(p, DATA_DIR / p) for p in sorted(paths)]
    jobs += [(p, DATA_DIR / "icons" / "site" / p) for p in SITE_ASSETS]

    def run(job):
        src, dest = job
        if dest.exists() and dest.stat().st_size > 0:
            return job, True
        data = fetch(f"{BASE_URL}/{src}", args.delay, binary=True)
        if not data:
            return job, False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return job, True

    failed = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for fut in as_completed([pool.submit(run, j) for j in jobs]):
            (src, dest), ok = fut.result()
            if not ok:
                failed.append(src)
                continue
            rel = dest.relative_to(DATA_DIR).as_posix()
            kind = rel.split("/")[1]
            db.execute("INSERT OR REPLACE INTO icons VALUES (?, ?, ?)",
                       (kind, icon_code(kind, rel), rel))
    db.commit()
    if failed:
        print(f"      {len(failed)} failed: {failed}", flush=True)


def step_export_json(db):
    print("[5/5] Writing cards.json ...", flush=True)
    icons = {(k, c): p for k, c, p in db.execute("SELECT kind, code, path FROM icons")}
    set_icons = dict(db.execute("SELECT code, icon FROM sets"))
    db.row_factory = sqlite3.Row
    cards = []
    for r in db.execute("SELECT * FROM cards ORDER BY id"):
        c = dict(r)
        cid = c["id"]
        c["civilizations"] = c["civilizations"].split("/") if c["civilizations"] else []
        c["races"] = c["races"].split("/") if c["races"] else []
        c["extra_fields"] = json.loads(c["extra_fields"]) if c["extra_fields"] else {}
        c["abilities"] = [dict(a) for a in db.execute(
            "SELECT text, reminder, keyword_icon FROM card_abilities WHERE card_id=? ORDER BY position", (cid,))]
        c["keywords"] = [k[0] for k in db.execute(
            "SELECT keyword FROM card_keywords WHERE card_id=? ORDER BY keyword", (cid,))]
        c["categories"] = [k[0] for k in db.execute(
            "SELECT category FROM card_categories WHERE card_id=? ORDER BY category", (cid,))]
        c["icons"] = {
            "civilizations": [icons.get(("civs", v.lower())) for v in c["civilizations"]],
            "rarity": icons.get(("rarities", c["rarity_code"])),
            "set": icons.get(("sets", set_icons.get(c["set_code"]))),
            "abilities": [icons.get(("keywords", a["keyword_icon"])) for a in c["abilities"]],
        }
        del c["detail_fetched"]
        cards.append(c)
    db.row_factory = None
    JSON_PATH.write_text(json.dumps(cards, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"      {len(cards)} cards -> {JSON_PATH}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="only process the first N cards (for testing)")
    ap.add_argument("--workers", type=int, default=3, help="parallel requests (default 3)")
    ap.add_argument("--delay", type=float, default=0.15,
                    help="minimum seconds between any two requests (default 0.15)")
    ap.add_argument("--skip-tags", action="store_true", help="skip keyword/category tagging")
    ap.add_argument("--skip-images", action="store_true")
    ap.add_argument("--videos", action="store_true", help="also download card videos")
    ap.add_argument("--refresh", action="store_true", help="re-fetch detail pages already scraped")
    args = ap.parse_args()

    db = open_db()
    started = time.time()
    search_html, card_ids = step_card_list(db, args)
    if not args.skip_tags:
        step_tags(db, args, search_html, card_ids)
    step_details(db, args, card_ids)

    step_icons(db, args, search_html)
    if not args.skip_images:
        rows =[(cid, IMG_URL.format(id=cid)) for cid in sorted(card_ids)]
        download_files(db, args, "[4/5] Downloading images", rows, IMG_DIR, "image_path")
    if args.videos:
        rows = db.execute("SELECT id, video_url FROM cards WHERE video_url IS NOT NULL").fetchall()
        rows = [r for r in rows if r[0] in card_ids]
        download_files(db, args, "[4b]  Downloading videos", rows, VIDEO_DIR, "video_path")

    step_export_json(db)
    db.close()
    print(f"Done in {time.time() - started:.0f}s. Database: {DB_PATH}")


if __name__ == "__main__":
    main()
