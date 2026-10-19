"""
Render saved decks as standalone HTML pages: one page per deck (card image grid + copyable
names) and an index page listing every deck.

The page uses relative paths to data/images and data/icons, so open it from inside the repo.
"""

import os
from html import escape
from pathlib import Path

COPY_ICON = ('<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" '
             'd="M16 1H4a2 2 0 0 0-2 2v14h2V3h12V1zm3 4H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 '
             '2-2V7a2 2 0 0 0-2-2zm0 16H8V7h11v14z"/></svg>')

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { --bg: #14161b; --panel: #1e2129; --text: #e8e8ea; --muted: #9a9ca3; --accent: #e8a33a; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  header { padding: 18px 32px 8px; }
  .crumbs { font-size: 13px; color: var(--muted); margin-bottom: 10px; display: flex; gap: 8px; }
  .crumbs a { color: var(--accent); text-decoration: none; }
  .crumbs a:hover { text-decoration: underline; }
  h1 { margin: 0 0 6px; font-size: 26px; }
  .meta { color: var(--muted); font-size: 14px; display: flex; gap: 14px; flex-wrap: wrap; align-items: center; }
  .meta img { width: 18px; height: 18px; vertical-align: middle; }
  details { margin: 12px 0 0; max-width: 900px; color: var(--muted); font-size: 14px; line-height: 1.5; }
  summary { cursor: pointer; color: var(--text); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
          gap: 20px; padding: 20px 32px 40px; }
  .card { background: var(--panel); border-radius: 10px; padding: 10px; display: flex;
          flex-direction: column; gap: 8px; }
  .art { position: relative; }
  .art img { width: 100%; aspect-ratio: 5 / 7; object-fit: cover; border-radius: 6px; display: block;
             background: #2a2e38; }
  .count { margin-left: auto; background: var(--accent); color: #1a1205; font-weight: 700;
           font-size: 13px; padding: 1px 9px; border-radius: 999px; }
  .info { color: var(--muted); font-size: 12px; display: flex; gap: 6px; align-items: center; }
  .info img { width: 14px; height: 14px; }
  .name { position: relative; }
  .name input { width: 100%; padding: 8px 36px 8px 10px; border-radius: 6px; border: 1px solid #343846;
                background: #12141a; color: var(--text); font-size: 13px; }
  .name button { position: absolute; right: 4px; top: 50%; transform: translateY(-50%);
                 border: 0; background: transparent; color: var(--muted); cursor: pointer;
                 padding: 5px; border-radius: 4px; display: flex; }
  .name button:hover { color: var(--text); background: #2a2e38; }
  .name button.copied { color: #4cc38a; }
  .toast { position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%); background: #4cc38a;
           color: #08130d; font-weight: 600; padding: 8px 16px; border-radius: 6px; opacity: 0;
           transition: opacity .2s; pointer-events: none; }
  .toast.show { opacity: 1; }
  .art img { cursor: zoom-in; transition: transform .15s; }
  .art img:hover { transform: scale(1.02); }
  .viewer { position: fixed; inset: 0; background: rgba(5, 6, 9, .9); display: none;
            flex-direction: column; align-items: center; justify-content: center; gap: 14px; z-index: 10; }
  .viewer.open { display: flex; }
  .viewer img { max-height: 84vh; max-width: 92vw; border-radius: 14px; box-shadow: 0 10px 50px rgba(0,0,0,.6);
                cursor: zoom-out; }
  .viewer .caption { color: var(--text); font-size: 16px; display: flex; gap: 10px; align-items: center; }
  .viewer button { position: absolute; border: 0; background: rgba(255,255,255,.1); color: #fff;
                   font-size: 26px; width: 48px; height: 48px; border-radius: 50%; cursor: pointer; }
  .viewer button:hover { background: rgba(255,255,255,.22); }
  .viewer .close { top: 18px; right: 18px; font-size: 22px; }
  .viewer .prev { left: 18px; top: 50%; transform: translateY(-50%); }
  .viewer .next { right: 18px; top: 50%; transform: translateY(-50%); }
</style>
</head>
<body>
<header>
  <nav class="crumbs" aria-label="Breadcrumb"><a href="index.html">All decks</a><span>&rsaquo;</span><span>__TITLE__</span></nav>
  <h1>__TITLE__</h1>
  <div class="meta">__META__</div>
  __STRATEGY__
</header>
<main class="grid">
__CARDS__
</main>
<div class="toast" id="toast">Copied</div>
<div class="viewer" id="viewer" role="dialog" aria-modal="true" aria-label="Card viewer">
  <button type="button" class="close" title="Close (Esc)" aria-label="Close">&#x2715;</button>
  <button type="button" class="prev" title="Previous (Left arrow)" aria-label="Previous card">&#x2039;</button>
  <img id="viewer-img" alt="">
  <div class="caption"><span id="viewer-name"></span><span class="count" id="viewer-count"></span></div>
  <button type="button" class="next" title="Next (Right arrow)" aria-label="Next card">&#x203A;</button>
</div>
<script>
  const toast = document.getElementById("toast");
  function fallbackCopy(input) {
    input.select();
    document.execCommand("copy");
    input.setSelectionRange(0, 0);
  }
  document.querySelectorAll(".name button").forEach(btn => {
    btn.addEventListener("click", async () => {
      const input = btn.previousElementSibling;
      try { await navigator.clipboard.writeText(input.value); } catch (e) { fallbackCopy(input); }
      btn.classList.add("copied");
      toast.textContent = "Copied: " + input.value;
      toast.classList.add("show");
      setTimeout(() => { btn.classList.remove("copied"); toast.classList.remove("show"); }, 1200);
    });
  });

  // Click a card to enlarge it; Esc / click closes, arrow keys step through the deck.
  const viewer = document.getElementById("viewer");
  const viewerImg = document.getElementById("viewer-img");
  const cards = Array.from(document.querySelectorAll(".card"));
  let current = -1;
  function show(i) {
    current = (i + cards.length) % cards.length;
    const card = cards[current];
    const img = card.querySelector(".art img");
    viewerImg.src = img.src;
    viewerImg.alt = img.alt;
    document.getElementById("viewer-name").textContent = img.alt;
    document.getElementById("viewer-count").textContent = card.querySelector(".count").textContent;
    viewer.classList.add("open");
  }
  function hide() { viewer.classList.remove("open"); current = -1; }
  cards.forEach((card, i) => card.querySelector(".art img").addEventListener("click", () => show(i)));
  viewer.querySelector(".close").addEventListener("click", hide);
  viewer.querySelector(".prev").addEventListener("click", e => { e.stopPropagation(); show(current - 1); });
  viewer.querySelector(".next").addEventListener("click", e => { e.stopPropagation(); show(current + 1); });
  viewer.addEventListener("click", e => { if (e.target === viewer || e.target === viewerImg) hide(); });
  document.addEventListener("keydown", e => {
    if (current < 0) return;
    if (e.key === "Escape") hide();
    else if (e.key === "ArrowLeft") show(current - 1);
    else if (e.key === "ArrowRight") show(current + 1);
  });
  // Open a card straight away with a link like deck.html#card-3
  const m = location.hash.match(/^#card-(\d+)$/);
  if (m && cards[m[1] - 1]) show(m[1] - 1);
</script>
</body>
</html>
"""

CARD = """  <div class="card">
    <div class="art"><img src="__IMG__" alt="__NAME__" loading="lazy"></div>
    <div class="info">__CIVS__<span>Cost __COST__ &middot; __TYPE__</span><span class="count">&times;__COUNT__</span></div>
    <div class="name"><input type="text" value="__NAME__" title="__NAME__" readonly aria-label="Card name"><button type="button" title="Copy name" aria-label="Copy name">__COPY__</button></div>
  </div>"""


def _rel(target, html_dir):
    try:
        return Path(os.path.relpath(target, html_dir)).as_posix()
    except ValueError:  # different drive on Windows: fall back to an absolute file:// URL
        return Path(target).resolve().as_uri()


def render_deck_html(deck, cards, icon_paths, data_dir, html_dir):
    """
    deck: saved deck dict. cards: [(card_dict, count)] in display order.
    icon_paths: {(kind, code): 'icons/...'} relative to data_dir.
    """
    def icon(kind, code, alt):
        p = icon_paths.get((kind, code))
        return f'<img src="{escape(_rel(Path(data_dir) / p, html_dir))}" alt="{escape(alt)}" title="{escape(alt)}">' if p else ""

    tiles = []
    for c, n in cards:
        img = _rel(Path(data_dir) / c["image_path"], html_dir) if c.get("image_path") else ""
        civs = "".join(icon("civs", civ.lower(), civ) for civ in c["civilizations"])
        tiles.append(CARD.replace("__IMG__", escape(img))
                         .replace("__NAME__", escape(c["name"]))
                         .replace("__COUNT__", str(n))
                         .replace("__CIVS__", civs)
                         .replace("__COST__", escape(str(c["cost"]) if c["cost"] is not None else "-"))
                         .replace("__TYPE__", escape(c["card_type"] or ""))
                         .replace("__COPY__", COPY_ICON))

    civ_meta = " ".join(icon("civs", civ.lower(), civ) + " " + escape(civ) for civ in deck.get("civilizations") or [])
    meta = [f"<span>{deck.get('total_cards', sum(n for _, n in cards))} cards</span>",
            f"<span>{len(cards)} unique</span>"]
    if civ_meta:
        meta.append(f"<span>{civ_meta}</span>")
    if deck.get("valid") is False:
        meta.append("<span style=\"color:#e5534b\">Not a legal deck</span>")

    strategy = ""
    if deck.get("strategy") or deck.get("notes"):
        parts = []
        if deck.get("strategy"):
            parts.append(f"<p><b>Strategy:</b> {escape(deck['strategy'])}</p>")
        if deck.get("notes"):
            parts.append(f"<p><b>Notes:</b> {escape(deck['notes'])}</p>")
        strategy = "<details><summary>Strategy &amp; notes</summary>" + "".join(parts) + "</details>"

    return (TEMPLATE.replace("__TITLE__", escape(deck.get("name") or deck.get("slug") or "Deck"))
                    .replace("__META__", "".join(meta))
                    .replace("__STRATEGY__", strategy)
                    .replace("__CARDS__", "\n".join(tiles)))


INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duel Masters decks</title>
<style>
  :root { --bg: #14161b; --panel: #1e2129; --text: #e8e8ea; --muted: #9a9ca3; --accent: #e8a33a; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  header { padding: 24px 32px 8px; display: flex; flex-wrap: wrap; gap: 16px; align-items: end;
           justify-content: space-between; }
  h1 { margin: 0; font-size: 26px; }
  .sub { color: var(--muted); font-size: 14px; margin-top: 4px; }
  #filter { padding: 9px 12px; width: 300px; border-radius: 6px; border: 1px solid #343846;
            background: #12141a; color: var(--text); font-size: 14px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
          gap: 20px; padding: 20px 32px 40px; }
  .deck { background: var(--panel); border-radius: 10px; overflow: hidden; display: flex;
          text-decoration: none; color: inherit; border: 1px solid transparent; transition: border-color .15s; }
  .deck:hover { border-color: var(--accent); }
  .cover { width: 100px; flex: none; background: #2a2e38; }
  .cover img { width: 100px; height: 140px; object-fit: cover; display: block; }
  .body { padding: 12px 14px; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .name { font-weight: 650; font-size: 16px; }
  .line { color: var(--muted); font-size: 12px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .line img { width: 15px; height: 15px; }
  .strategy { color: var(--muted); font-size: 12px; line-height: 1.4; overflow: hidden;
              display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; }
  .bad { color: #e5534b; }
  .empty { color: var(--muted); padding: 20px 32px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>Duel Masters decks</h1>
    <div class="sub">__COUNT__</div>
  </div>
  <input id="filter" type="search" placeholder="Filter by name, civilization or card..." aria-label="Filter decks">
</header>
<main class="grid">
__DECKS__
</main>
__EMPTY__
<script>
  const filter = document.getElementById("filter");
  filter.addEventListener("input", () => {
    const q = filter.value.trim().toLowerCase();
    document.querySelectorAll(".deck").forEach(d => {
      d.style.display = !q || d.dataset.search.includes(q) ? "" : "none";
    });
  });
</script>
</body>
</html>
"""

INDEX_DECK = """  <a class="deck" href="__HREF__" data-search="__SEARCH__">
    <div class="cover">__COVER__</div>
    <div class="body">
      <div class="name">__NAME__</div>
      <div class="line">__CIVS__<span>__INFO__</span></div>
      <div class="strategy">__STRATEGY__</div>
    </div>
  </a>"""


def render_index_html(entries, icon_paths, data_dir, html_dir):
    """
    entries: [{"file", "name", and optionally "civilizations", "total_cards", "valid", "updated",
               "strategy", "cover_image" (relative to data_dir), "card_names"}]
    """
    def icon(kind, code, alt):
        p = icon_paths.get((kind, code))
        return f'<img src="{escape(_rel(Path(data_dir) / p, html_dir))}" alt="{escape(alt)}" title="{escape(alt)}">' if p else ""

    tiles = []
    for e in entries:
        cover = ""
        if e.get("cover_image"):
            cover = f'<img src="{escape(_rel(Path(data_dir) / e["cover_image"], html_dir))}" alt="" loading="lazy">'
        info = []
        if e.get("total_cards"):
            info.append(f"{e['total_cards']} cards")
        if e.get("updated"):
            info.append(f"updated {escape(e['updated'][:10])}")
        info_html = " &middot; ".join(info)
        if e.get("valid") is False:
            info_html += ' <span class="bad">(not legal)</span>'
        civs = e.get("civilizations") or []
        search = " ".join([e["name"], *civs, *(e.get("card_names") or [])]).lower()
        tiles.append(INDEX_DECK.replace("__HREF__", escape(e["file"]))
                               .replace("__SEARCH__", escape(search))
                               .replace("__COVER__", cover)
                               .replace("__NAME__", escape(e["name"]))
                               .replace("__CIVS__", "".join(icon("civs", c.lower(), c) for c in civs))
                               .replace("__INFO__", info_html)
                               .replace("__STRATEGY__", escape(e.get("strategy") or "")))
    count = f"{len(entries)} deck{'s' if len(entries) != 1 else ''}"
    empty = "" if entries else '<p class="empty">No decks yet. Ask Claude to build one.</p>'
    return (INDEX_TEMPLATE.replace("__COUNT__", count)
                          .replace("__DECKS__", "\n".join(tiles))
                          .replace("__EMPTY__", empty))
