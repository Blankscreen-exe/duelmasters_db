"""
Card queries, deck analysis and deck storage for the Duel Masters MCP server.

Kept free of any MCP/protocol code so the game (or tests) can import it directly.
"""

import json
import re
import sqlite3
import webbrowser
from collections import Counter
from datetime import datetime, timezone
from difflib import get_close_matches
from pathlib import Path

from deck_html import render_deck_html, render_index_html

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "duelmasters.db"
DECKS_DIR = DATA_DIR / "decks"
HTML_DIR = ROOT / "decks"  # browsable HTML deck pages

# House rule for this game: 40-50 cards are legal, 40 is ideal (official Duel Masters is exactly 40).
DECK_MIN = 40
DECK_MAX = 50
DECK_IDEAL = 40
MAX_COPIES = 4
CIVILIZATIONS = ["Light", "Water", "Darkness", "Fire", "Nature"]
MAX_SEARCH_LIMIT = 100


class DMError(Exception):
    """Error meant to be shown to the caller (bad input, unknown card, ...)."""


# --------------------------------------------------------------------------
# Database access
# --------------------------------------------------------------------------

class CardDB:
    def __init__(self, db_path=DB_PATH, decks_dir=DECKS_DIR, html_dir=HTML_DIR):
        if not Path(db_path).exists():
            raise DMError(f"Database not found at {db_path}. Run scraper/scrape_dm.py first.")
        self.db = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.decks_dir = Path(decks_dir)
        self.html_dir = Path(html_dir)
        self._load_cache()

    def _load_cache(self):
        """Everything fits in memory (~2.6k cards), so load once and query in Python."""
        q = self.db.execute
        self.keywords = dict(q("SELECT code, label FROM keywords").fetchall())
        self.categories = {r["code"]: {"label": r["label"], "group": r["group_name"]}
                           for r in q("SELECT * FROM categories")}
        self.races = sorted({r[0] for r in q("SELECT DISTINCT race FROM card_races")},
                            key=len, reverse=True)
        kw = {}
        for cid, k in q("SELECT card_id, keyword FROM card_keywords"):
            kw.setdefault(cid, []).append(k)
        cat = {}
        for cid, c in q("SELECT card_id, category FROM card_categories"):
            cat.setdefault(cid, []).append(c)
        abil = {}
        for r in q("SELECT card_id, text, reminder, keyword_icon FROM card_abilities ORDER BY card_id, position"):
            abil.setdefault(r["card_id"], []).append(
                {"text": r["text"], "reminder": r["reminder"], "keyword_icon": r["keyword_icon"]})

        self.cards = {}
        for r in q("SELECT * FROM cards ORDER BY id"):
            c = dict(r)
            c["civilizations"] = c["civilizations"].split("/") if c["civilizations"] else []
            c["races"] = c["races"].split("/") if c["races"] else []
            c["keywords"] = sorted(kw.get(c["id"], []))
            c["categories"] = sorted(cat.get(c["id"], []))
            c["abilities"] = abil.get(c["id"], [])
            c["rules_text"] = c["rules_text"] or ""
            c["evolution"] = parse_evolution(c, self.races)
            self.cards[c["id"]] = c

        self.by_name = {}
        for c in self.cards.values():
            self.by_name.setdefault(c["name"].lower(), []).append(c["id"])

    # ---------------------------------------------------------------- lookup

    def resolve(self, ref):
        """Card id ('0079', '79') or exact name (case-insensitive) -> card dict."""
        ref = str(ref).strip()
        if ref.isdigit() and ref.zfill(4) in self.cards:
            return self.cards[ref.zfill(4)]
        ids = self.by_name.get(ref.lower())
        if ids:
            return self.cards[ids[0]]  # reprints share a name; first printing is canonical
        partial = [n for n in self.by_name if ref.lower() in n]
        if len(partial) == 1:
            return self.cards[self.by_name[partial[0]][0]]
        suggestions = partial[:8] or get_close_matches(ref.lower(), self.by_name.keys(), n=5, cutoff=0.6)
        hint = f" Did you mean: {', '.join(self._display_name(s) for s in suggestions)}?" if suggestions else ""
        raise DMError(f"Unknown card '{ref}'.{hint}")

    def _display_name(self, lower_name):
        return self.cards[self.by_name[lower_name][0]]["name"]

    # ---------------------------------------------------------------- output shapes

    @staticmethod
    def card_summary(c):
        """Compact card shape for search results (reminder text stripped)."""
        out = {
            "id": c["id"],
            "name": c["name"],
            "civilizations": c["civilizations"],
            "cost": c["cost"],
            "type": c["card_type"],
            "races": c["races"],
            "power": c["power_text"] or None,
            "keywords": c["keywords"],
            "text": strip_reminders(c["rules_text"]),
            "set": c["set_code"],
        }
        return {k: v for k, v in out.items() if v not in (None, [], "")}

    def card_detail(self, c):
        reprints = [i for i in self.by_name[c["name"].lower()] if i != c["id"]]
        out = {
            "id": c["id"],
            "name": c["name"],
            "civilizations": c["civilizations"],
            "cost": c["cost"],
            "type": c["card_type"],
            "races": c["races"],
            "power": c["power_text"] or None,
            "rarity": c["rarity"],
            "set": c["set_code"],
            "collector_number": c["collector_number"],
            "abilities": [a["text"] for a in c["abilities"]],
            "keywords": [{"code": k, "label": self.keywords.get(k, k)} for k in c["keywords"]],
            "categories": [{"code": k, "label": self.categories.get(k, {}).get("label", k)}
                           for k in c["categories"]],
            "evolution_requirement": c["evolution"],
            "reprint_ids": reprints,
            "image_path": f"data/{c['image_path']}" if c["image_path"] else None,
            "wiki_url": c["wiki_url"],
        }
        return {k: v for k, v in out.items() if v not in (None, [], "")}

    # ---------------------------------------------------------------- tools

    def search(self, query=None, text=None, civilizations=None, civ_match="any",
               card_types=None, races=None, keywords=None, categories=None,
               cost_min=None, cost_max=None, power_min=None, power_max=None,
               sets=None, rarity=None, include_reprints=False, sort="cost",
               limit=25, offset=0):
        civs = _norm_civs(civilizations)
        types = {t.lower() for t in (card_types or [])}
        race_terms = [r.lower() for r in (races or [])]
        kws = self._check_codes(keywords, self.keywords, "keywords")
        cats = self._check_codes(categories, self.categories, "categories")
        set_codes = {s.upper() for s in (sets or [])}
        if civ_match not in ("any", "all", "exact"):
            raise DMError("civ_match must be 'any', 'all' or 'exact'.")

        results, seen_names = [], set()
        for c in self.cards.values():
            if query and query.lower() not in c["name"].lower():
                continue
            if text and text.lower() not in c["rules_text"].lower():
                continue
            if civs:
                cc = set(c["civilizations"])
                if civ_match == "any" and not cc & civs:
                    continue
                if civ_match == "all" and not civs <= cc:
                    continue
                if civ_match == "exact" and cc != civs:
                    continue
            if types and c["card_type"].lower() not in types:
                continue
            if race_terms and not any(t in r.lower() for t in race_terms for r in c["races"]):
                continue
            if kws and not kws <= set(c["keywords"]):
                continue
            if cats and not cats <= set(c["categories"]):
                continue
            if cost_min is not None and (c["cost"] is None or c["cost"] < cost_min):
                continue
            if cost_max is not None and (c["cost"] is None or c["cost"] > cost_max):
                continue
            if power_min is not None and (c["power"] is None or c["power"] < power_min):
                continue
            if power_max is not None and (c["power"] is None or c["power"] > power_max):
                continue
            if set_codes and (c["set_code"] or "").upper() not in set_codes:
                continue
            if rarity and rarity.lower() not in ((c["rarity"] or "").lower(), (c["rarity_code"] or "").lower()):
                continue
            if not include_reprints:
                if c["name"] in seen_names:
                    continue
                seen_names.add(c["name"])
            results.append(c)

        keyfuncs = {
            "cost": lambda c: (c["cost"] if c["cost"] is not None else 99, c["name"]),
            "power": lambda c: (-(c["power"] or 0), c["name"]),
            "name": lambda c: c["name"],
            "id": lambda c: c["id"],
        }
        if sort not in keyfuncs:
            raise DMError(f"sort must be one of {list(keyfuncs)}.")
        results.sort(key=keyfuncs[sort])
        limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        page = results[offset:offset + limit]
        return {
            "total_matches": len(results),
            "returned": len(page),
            "offset": offset,
            "note": "Reminder text is stripped; use get_cards for full details.",
            "cards": [self.card_summary(c) for c in page],
        }

    def get_cards(self, cards):
        if not cards:
            raise DMError("Pass at least one card id or name.")
        if len(cards) > 60:
            raise DMError("At most 60 cards per call.")
        out, errors = [], []
        for ref in cards:
            try:
                out.append(self.card_detail(self.resolve(ref)))
            except DMError as e:
                errors.append(str(e))
        return {"cards": out, "errors": errors} if errors else {"cards": out}

    def list_values(self, kind):
        q = self.db.execute
        if kind == "civilizations":
            rows = q("SELECT civilization, COUNT(*) FROM card_civilizations GROUP BY 1 ORDER BY 2 DESC")
            return [{"value": r[0], "cards": r[1]} for r in rows]
        if kind == "card_types":
            rows = q("SELECT card_type, COUNT(*) FROM cards GROUP BY 1 ORDER BY 2 DESC")
            return [{"value": r[0], "cards": r[1]} for r in rows]
        if kind == "races":
            rows = q("SELECT race, COUNT(*) FROM card_races GROUP BY 1 ORDER BY 1")
            return [{"value": r[0], "cards": r[1]} for r in rows]
        if kind == "keywords":
            rows = q("""SELECT k.code, k.label, COUNT(ck.card_id) FROM keywords k
                        LEFT JOIN card_keywords ck ON ck.keyword = k.code GROUP BY 1 ORDER BY 2""")
            return [{"value": r[0], "label": r[1], "cards": r[2]} for r in rows]
        if kind == "categories":
            rows = q("""SELECT c.code, c.label, c.group_name, COUNT(cc.card_id) FROM categories c
                        LEFT JOIN card_categories cc ON cc.category = c.code GROUP BY 1 ORDER BY 3, 2""")
            return [{"value": r[0], "label": r[1], "group": r[2], "cards": r[3]} for r in rows]
        if kind == "sets":
            rows = q("SELECT set_code, COUNT(*), MIN(id) FROM cards GROUP BY 1 ORDER BY 1")
            return [{"value": r[0], "cards": r[1]} for r in rows]
        if kind == "rarities":
            rows = q("SELECT rarity_code, rarity, COUNT(*) FROM cards GROUP BY 1, 2 ORDER BY 3")
            return [{"value": r[0], "label": r[1], "cards": r[2]} for r in rows if r[0]]
        raise DMError("kind must be one of: civilizations, card_types, races, keywords, categories, sets, rarities.")

    def find_synergies(self, card, limit=15):
        c = self.resolve(card)
        limit = max(1, min(int(limit), 50))
        out = {"card": self.card_summary(c)}

        if c["evolution"]:
            bases = [x for x in self.unique_cards() if x["id"] != c["id"]
                     and is_valid_base(x, c["evolution"])]
            bases.sort(key=lambda x: (not set(x["civilizations"]) & set(c["civilizations"]),
                                      x["cost"] or 99, x["name"]))
            out["evolution_requirement"] = c["evolution"]
            out["evolution_bases"] = {"total": len(bases),
                                      "cards": [self.card_summary(x) for x in bases[:limit]]}

        if "creature" in c["card_type"].lower():
            evos = [x for x in self.unique_cards()
                    if x["evolution"] and x["id"] != c["id"] and is_valid_base(c, x["evolution"])
                    and not x["evolution"].get("unparsed")]
            evos.sort(key=lambda x: (not set(x["civilizations"]) & set(c["civilizations"]),
                                     x["cost"] or 99, x["name"]))
            out["evolves_into"] = {"total": len(evos),
                                   "cards": [self.card_summary(x) for x in evos[:limit]]}

        if c["races"]:
            terms = set()
            for r in c["races"]:
                terms.add(r.lower())
                if "dragon" in r.lower():
                    terms.add("dragon")
            support = []
            for x in self.unique_cards():
                if x["id"] == c["id"] or not x["rules_text"]:
                    continue
                body = strip_reminders(x["rules_text"]).lower()
                if x["evolution"]:  # ignore the evolution clause itself
                    body = body.replace(x["evolution"]["text"].lower(), "")
                if any(re.search(rf"\b{re.escape(t)}", body) for t in terms):
                    support.append(x)
            support.sort(key=lambda x: (not set(x["civilizations"]) & set(c["civilizations"]),
                                        x["cost"] or 99, x["name"]))
            out["race_support"] = {
                "note": f"Cards whose rules text mentions {sorted(terms)} (excluding evolution clauses).",
                "total": len(support),
                "cards": [self.card_summary(x) for x in support[:limit]],
            }

        named = [x for x in self.unique_cards()
                 if x["id"] != c["id"] and c["name"].lower() in x["rules_text"].lower()]
        if named:
            out["mentions_by_name"] = [self.card_summary(x) for x in named[:limit]]
        return out

    def unique_cards(self):
        """One printing per card name."""
        return (self.cards[ids[0]] for ids in self.by_name.values())

    # ---------------------------------------------------------------- decks

    def resolve_deck(self, entries):
        """[{'card': id-or-name, 'count': n}] -> ([(card, count)], errors). Merges duplicates by name."""
        if not isinstance(entries, list) or not entries:
            raise DMError("cards must be a non-empty list of {card, count} objects.")
        merged, order, errors = {}, [], []
        for e in entries:
            if isinstance(e, str):
                e = {"card": e, "count": 1}
            ref = e.get("card") or e.get("id") or e.get("name")
            try:
                count = int(e.get("count", 1))
            except (TypeError, ValueError):
                errors.append(f"Invalid count for '{ref}'.")
                continue
            if count < 1:
                errors.append(f"Count for '{ref}' must be at least 1.")
                continue
            try:
                c = self.resolve(ref)
            except DMError as ex:
                errors.append(str(ex))
                continue
            key = c["name"].lower()
            if key not in merged:
                merged[key] = [c, 0]
                order.append(key)
            merged[key][1] += count
        return [tuple(merged[k]) for k in order], errors

    def validate_deck(self, cards):
        resolved, errors = self.resolve_deck(cards)
        total = sum(n for _, n in resolved)
        if not DECK_MIN <= total <= DECK_MAX:
            errors.append(f"Deck has {total} cards; it must have {DECK_MIN}-{DECK_MAX}.")
        for c, n in resolved:
            if n > MAX_COPIES:
                errors.append(f"{n} copies of '{c['name']}'; the maximum is {MAX_COPIES}.")
        return {"valid": not errors, "total_cards": total, "errors": errors}

    def analyze_deck(self, cards):
        resolved, errors = self.resolve_deck(cards)
        validation = self.validate_deck(cards)
        total = sum(n for _, n in resolved)
        if not resolved:
            return {"validation": validation}

        curve = Counter()
        civ_counts = Counter()
        type_counts = Counter()
        kw_counts = Counter()
        cat_counts = Counter()
        race_counts = Counter()
        multicolor = 0
        costs = []
        for c, n in resolved:
            cost = c["cost"] if c["cost"] is not None else 0
            curve["7+" if cost >= 7 else str(cost)] += n
            costs += [cost] * n
            for civ in c["civilizations"]:
                civ_counts[civ] += n
            if len(c["civilizations"]) > 1:
                multicolor += n
            type_counts[c["card_type"]] += n
            for k in c["keywords"]:
                kw_counts[k] += n
            for k in c["categories"]:
                cat_counts[k] += n
            for r in c["races"]:
                race_counts[r] += n

        creatures = sum(n for c, n in resolved if "creature" in c["card_type"].lower())
        early = sum(n for c, n in resolved if (c["cost"] or 0) <= 3)

        # Evolution base check
        evolutions = []
        for c, n in resolved:
            if not c["evolution"]:
                continue
            req = c["evolution"]
            if req.get("unparsed"):
                evolutions.append({"card": c["name"], "count": n, "requirement": req["text"],
                                   "bases_in_deck": None, "note": "Could not parse requirement; check manually."})
                continue
            bases = sum(m for b, m in resolved if b is not c and is_valid_base(b, req))
            evolutions.append({"card": c["name"], "count": n, "requirement": req["text"],
                               "bases_in_deck": bases})

        warnings = []
        if DECK_IDEAL < total <= DECK_MAX:
            warnings.append(f"{total} cards; {DECK_IDEAL} is ideal. Each extra card makes your key cards "
                            f"less likely to show up, so only add cards that earn their slot.")
        st = kw_counts.get("shield_trigger", 0)
        if st < 4:
            warnings.append(f"Only {st} Shield Trigger cards; most decks want 6-12 for defense.")
        if early < 10:
            warnings.append(f"Only {early} cards cost 3 or less; the deck may start too slowly.")
        avg = sum(costs) / len(costs)
        if avg > 5:
            warnings.append(f"Average cost {avg:.1f} is high; make sure there's ramp or early defense.")
        if len(civ_counts) > 3:
            warnings.append(f"{len(civ_counts)} civilizations; 2 is most consistent, 3 needs extra mana support.")
        if len(civ_counts) >= 2:
            for civ, n in civ_counts.items():
                if n < 8:
                    warnings.append(f"Only {n} {civ} cards; you may not have {civ} mana when you need it.")
        if multicolor > 12:
            warnings.append(f"{multicolor} multicolor cards; they enter the mana zone tapped, which slows early turns.")
        if creatures < 12 and type_counts:
            warnings.append(f"Only {creatures} creatures; the deck may lack attackers to break shields.")
        for e in evolutions:
            if e["bases_in_deck"] is not None and e["bases_in_deck"] < max(6, e["count"] * 2):
                warnings.append(f"'{e['card']}' x{e['count']} has only {e['bases_in_deck']} valid bases "
                                f"({e['requirement']}).")

        roles = {
            "shield_triggers": st,
            "blockers": kw_counts.get("blocker", 0),
            "speed_attackers": kw_counts.get("speed_attack", 0),
            "removal": cat_counts.get("removal", 0),
            "card_draw": cat_counts.get("draw", 0),
            "ramp": cat_counts.get("ramp", 0),
            "double_breaker_or_better": sum(kw_counts.get(k, 0) for k in
                                            ("double_break", "triple_break", "quattro_breaker", "world_breaker")),
        }
        return {
            "validation": validation,
            "total_cards": total,
            "unique_cards": len(resolved),
            "average_cost": round(avg, 2),
            "mana_curve": {k: curve[k] for k in ["0", "1", "2", "3", "4", "5", "6", "7+"] if curve[k]},
            "civilizations": dict(civ_counts.most_common()),
            "multicolor_cards": multicolor,
            "card_types": dict(type_counts.most_common()),
            "roles": roles,
            "top_races": dict(race_counts.most_common(6)),
            "keywords": {self.keywords.get(k, k): v for k, v in kw_counts.most_common()},
            "categories": {self.categories.get(k, {}).get("label", k): v for k, v in cat_counts.most_common()},
            "evolutions": evolutions,
            "warnings": warnings,
            "cards": [{"id": c["id"], "name": c["name"], "count": n, "cost": c["cost"],
                       "civilizations": c["civilizations"], "type": c["card_type"]}
                      for c, n in sorted(resolved, key=lambda x: (x[0]["cost"] or 0, x[0]["name"]))],
        }

    # ---------------------------------------------------------------- deck storage

    def _deck_path(self, slug):
        slug = slugify(slug)
        if not slug:
            raise DMError("Deck name must contain letters or numbers.")
        if slug == "index":
            raise DMError("'index' is reserved for the deck listing page; pick another deck name.")
        return self.decks_dir / f"{slug}.json"

    def save_deck(self, name, cards, strategy=None, notes=None, overwrite=False, allow_invalid=False):
        validation = self.validate_deck(cards)
        if not validation["valid"] and not allow_invalid:
            raise DMError("Deck is not valid, not saved: " + " ".join(validation["errors"])
                          + " (Pass allow_invalid=true to save a draft anyway.)")
        resolved, _ = self.resolve_deck(cards)
        path = self._deck_path(name)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        created = now
        if path.exists():
            if not overwrite:
                raise DMError(f"A deck named '{path.stem}' already exists. Pass overwrite=true to replace it.")
            created = json.loads(path.read_text(encoding="utf-8")).get("created", now)
        civs = Counter()
        for c, n in resolved:
            for civ in c["civilizations"]:
                civs[civ] += n
        deck = {
            "slug": path.stem,
            "name": name,
            "strategy": strategy,
            "notes": notes,
            "valid": validation["valid"],
            "total_cards": validation["total_cards"],
            "civilizations": [c for c, _ in civs.most_common()],
            "created": created,
            "updated": now,
            "cards": [{"id": c["id"], "name": c["name"], "count": n,
                       "image_path": f"data/{c['image_path']}" if c["image_path"] else None}
                      for c, n in resolved],
        }
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(deck, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        try:
            shown = path.relative_to(ROOT).as_posix()
        except ValueError:  # decks dir outside the repo
            shown = str(path)
        html = self.export_deck_html(path.stem)
        return {"saved": True, "slug": path.stem, "path": shown, "html_path": html["html_path"],
                "valid": validation["valid"], "errors": validation["errors"]}

    def list_decks(self):
        if not self.decks_dir.exists():
            return []
        out = []
        for p in sorted(self.decks_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                out.append({"slug": p.stem, "error": "unreadable deck file"})
                continue
            out.append({"slug": p.stem, "name": d.get("name"), "civilizations": d.get("civilizations"),
                        "total_cards": d.get("total_cards"), "valid": d.get("valid"),
                        "updated": d.get("updated"), "strategy": (d.get("strategy") or "")[:160] or None})
        return out

    def get_deck(self, slug):
        path = self._deck_path(slug)
        if not path.exists():
            raise DMError(f"No deck named '{slug}'. Use list_decks to see saved decks.")
        return json.loads(path.read_text(encoding="utf-8"))

    def delete_deck(self, slug):
        path = self._deck_path(slug)
        if not path.exists():
            raise DMError(f"No deck named '{slug}'.")
        path.unlink()
        html = self.html_dir / f"{path.stem}.html"
        if html.exists():
            html.unlink()
        self.export_index_html()
        return {"deleted": path.stem}

    def export_deck_html(self, slug):
        """Write decks/<slug>.html: card image grid with one-click copyable names."""
        deck = self.get_deck(slug)
        cards = []
        for entry in deck["cards"]:
            c = self.cards.get(entry["id"]) or self.resolve(entry["name"])
            cards.append((c, entry["count"]))
        cards.sort(key=lambda x: (x[0]["cost"] if x[0]["cost"] is not None else 99, x[0]["name"]))
        icons = {(r["kind"], r["code"]): r["path"] for r in self.db.execute("SELECT kind, code, path FROM icons")}
        self.html_dir.mkdir(parents=True, exist_ok=True)
        out = self.html_dir / f"{deck['slug']}.html"
        out.write_text(render_deck_html(deck, cards, icons, DATA_DIR, self.html_dir), encoding="utf-8")
        self.export_index_html()
        return {"html_path": self._shown(out), "absolute_path": str(out), "cards": len(cards)}

    def export_index_html(self):
        """Write decks/index.html listing every deck page in the decks folder, newest first."""
        self.html_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        for page in self.html_dir.glob("*.html"):
            if page.stem == "index":
                continue
            entry = {"file": page.name, "name": page.stem.replace("-", " ").title()}
            data_file = self.decks_dir / f"{page.stem}.json"
            try:
                d = json.loads(data_file.read_text(encoding="utf-8")) if data_file.exists() else None
            except (OSError, json.JSONDecodeError):
                d = None
            if d:
                resolved = [(self.cards.get(c.get("id")), c.get("count", 1)) for c in d.get("cards", [])]
                resolved = [(c, n) for c, n in resolved if c]
                # Cover image: the strongest creature (highest power, then cost), else the first card.
                creatures = [c for c, _ in resolved if c["power"] is not None]
                cover = (max(creatures, key=lambda c: (c["power"], c["cost"] or 0)) if creatures
                         else resolved[0][0] if resolved else None)
                entry.update(name=d.get("name") or entry["name"], civilizations=d.get("civilizations"),
                             total_cards=d.get("total_cards"), valid=d.get("valid"),
                             updated=d.get("updated"), strategy=d.get("strategy"),
                             cover_image=cover["image_path"] if cover else None,
                             card_names=[c["name"] for c, _ in resolved])
            entries.append(entry)
        entries.sort(key=lambda e: (e.get("updated") or "", e["name"]), reverse=True)
        icons = {(r["kind"], r["code"]): r["path"] for r in self.db.execute("SELECT kind, code, path FROM icons")}
        out = self.html_dir / "index.html"
        out.write_text(render_index_html(entries, icons, DATA_DIR, self.html_dir), encoding="utf-8")
        return {"index_path": self._shown(out), "absolute_path": str(out), "decks": len(entries)}

    def open_deck_listing(self, open_browser=True):
        """Rebuild decks/index.html and open it in the default browser."""
        result = self.export_index_html()
        if open_browser:
            result["opened_in_browser"] = webbrowser.open(Path(result["absolute_path"]).as_uri())
        return result

    @staticmethod
    def _shown(path):
        try:
            return Path(path).relative_to(ROOT).as_posix()
        except ValueError:
            return str(path)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _check_codes(values, known, kind):
        if not values:
            return set()
        unknown = [v for v in values if v not in known]
        if unknown:
            raise DMError(f"Unknown {kind} code(s): {unknown}. Use list_values with kind='{kind}' "
                          f"to see valid codes.")
        return set(values)


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def strip_reminders(text):
    """Drop parenthesised reminder text, e.g. 'Blocker (Whenever ...)' -> 'Blocker'."""
    text = re.sub(r"\s*\([^()]*\)", "", text or "")
    return "\n".join(l.strip() for l in text.split("\n") if l.strip())


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:80]


def _norm_civs(civs):
    out = set()
    for c in civs or []:
        match = [x for x in CIVILIZATIONS if x.lower() == str(c).lower()]
        if not match:
            raise DMError(f"Unknown civilization '{c}'. Valid: {CIVILIZATIONS}.")
        out.add(match[0])
    return out


# --------------------------------------------------------------------------
# Evolution requirement parsing (best effort; the raw text is always kept)
# --------------------------------------------------------------------------

def parse_evolution(card, races):
    if "evolution" not in card["card_type"].lower():
        return None
    line = next((l for l in card["rules_text"].split("\n")
                 if "evolution" in l.lower() and ("put" in l.lower() or "choose" in l.lower())), None)
    if line is None:
        return {"text": None, "unparsed": True}
    lower = line.lower()
    req = {"text": line}
    if lower.startswith("mana"):
        req["zone"] = "mana"
    elif lower.startswith("graveyard"):
        req["zone"] = "graveyard"
    elif lower.startswith("hand"):
        req["zone"] = "hand"
    elif lower.startswith("deck"):
        req["zone"] = "deck"
    if "vortex" in lower:
        req["vortex"] = True

    # Only look at the clause after the dash/colon that names the base.
    clause = re.split(r"[—:]", line, maxsplit=1)[-1]
    found, rest = [], clause
    for race in races:  # longest first, so 'Armored Dragon' wins over 'Dragon'
        pat = re.compile(rf"\b{re.escape(race)}", re.IGNORECASE)
        if pat.search(rest):
            found.append(race)
            rest = pat.sub(" ", rest)
    m = re.search(r"has ([\w' ]+?) in its race", clause, re.IGNORECASE)
    contains = []
    if m:
        contains.append(m.group(1).strip())
    elif re.search(r"\bdragons?\b", rest, re.IGNORECASE):
        contains.append("Dragon")
    else:
        # 'one of your Cyber creatures' -> any race containing 'Cyber'
        m = re.search(r"your ([A-Z][\w']+) creatures", rest)
        if m and m.group(1).capitalize() not in CIVILIZATIONS + ["Evolution"]:
            contains.append(m.group(1))
    if found:
        req["races"] = found
    if contains:
        req["race_contains"] = contains
    # Civ words are checked after race names are removed, so 'Fire Bird' isn't read as Fire.
    civs = [c for c in CIVILIZATIONS if re.search(rf"\b{c}\b", rest, re.IGNORECASE)]
    if civs:
        req["civilizations"] = civs
    if not (found or contains or civs) and (
            re.search(r"of your creatures|revealed creature|from the revealed cards", clause, re.IGNORECASE)):
        req["any_creature"] = True
    if re.search(r"evolution creature", clause, re.IGNORECASE):
        req["base_is_evolution"] = True
    if "multicolored" in clause.lower():
        req["multicolored"] = True
    if '"blocker"' in clause.lower():
        req["keyword"] = "blocker"
    m = re.search(r'has "([^"]+)" in its name', clause)
    if m:
        req["name_contains"] = m.group(1)
    if not any(k in req for k in ("races", "race_contains", "civilizations", "base_is_evolution",
                                  "multicolored", "keyword", "name_contains", "any_creature")):
        req["unparsed"] = True
    return req


def is_valid_base(card, req):
    """Can `card` satisfy evolution requirement `req`? (best effort)"""
    if not req or req.get("unparsed") or "creature" not in card["card_type"].lower():
        return False
    is_evo = "evolution" in card["card_type"].lower()
    if req.get("base_is_evolution") and not is_evo:
        return False
    if req.get("any_creature"):
        return True
    checks = []
    if req.get("races") or req.get("race_contains"):
        exact = {r.lower() for r in req.get("races", [])}
        parts = [p.lower() for p in req.get("race_contains", [])]
        checks.append(any(r.lower() in exact or any(p in r.lower() for p in parts) for r in card["races"]))
    elif req.get("civilizations"):
        checks.append(bool(set(req["civilizations"]) & set(card["civilizations"])))
    if req.get("multicolored"):
        checks.append(len(card["civilizations"]) > 1)
    if req.get("keyword"):
        checks.append(req["keyword"] in card["keywords"])
    if req.get("name_contains"):
        checks.append(req["name_contains"].lower() in card["name"].lower())
    if req.get("base_is_evolution") and not checks:
        return True  # 'Put on one of your evolution creatures'
    return bool(checks) and all(checks)
