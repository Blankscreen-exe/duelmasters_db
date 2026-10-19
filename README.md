# Duel Masters local card database

Local mirror of https://db.duelmasters.us/ (2,647 cards, DM-01 → DM-32 + promos).

## Re-running the scraper

```
python scraper/scrape_dm.py              # full scrape, resumable (skips images/details already saved)
python scraper/scrape_dm.py --refresh    # re-fetch every detail page
python scraper/scrape_dm.py --videos     # also grab the card videos (.mp4)
python scraper/scrape_dm.py --limit 20   # quick test
```

Standard-library Python only, no `pip install` needed.

## Output

| Path | What |
|---|---|
| `data/duelmasters.db` | SQLite database |
| `data/images/<id>.webp` | Card images, named by card id |
| `data/icons/` | Site icons: `civs/` (5), `rarities/` (5), `sets/` (35 booster/set icons), `keywords/` (16 ability bullets), `site/` (banner, favicon) |
| `data/cards.json` | Same data as one JSON array (easy to load into Unity/Godot/JS) |

## Schema

- **cards**: one row per card: `id`, `name`, `card_type`, `cost`, `power`, `civilizations`, `races`,
  `rarity`, `set_code`, `collector_number`, `rules_text`, `image_path`, `video_url`, `wiki_url`
- **card_abilities**: each rules-text line in order (`text`, `reminder`, `keyword_icon`)
- **card_civilizations** / **card_races**: one row per civ/race (for multi-civ and multi-race cards)
- **keywords** + **card_keywords**: Blocker, Shield Trigger, Double Breaker, Speed Attacker, ...
- **categories** + **card_categories**: effect tags such as removal, draw, ramp, triggers, ...
- **sets**
- **icons**: `(kind, code, path)` lookup. `code` matches `lower(civilization)`, `cards.rarity_code`,
  `sets.icon`, or `card_abilities.keyword_icon`. In `cards.json`, each card also has an `icons` object
  with these paths already filled in.

Not every value has an icon: the site has none for cards without a rarity (93 cards) or for the
28 keywords that never show a bullet icon.

`card_type` is normalized: the site has typos like "Creatue", but the raw value is kept in `card_type_raw`.

## Example queries

```sql
-- All fire blockers that cost 3 or less
SELECT c.name, c.cost, c.power FROM cards c
JOIN card_civilizations cv ON cv.card_id = c.id AND cv.civilization = 'Fire'
JOIN card_keywords k ON k.card_id = c.id AND k.keyword = 'blocker'
WHERE c.cost <= 3;

-- Spells with Shield Trigger
SELECT c.name, c.rules_text FROM cards c
JOIN card_keywords k ON k.card_id = c.id AND k.keyword = 'shield_trigger'
WHERE c.card_type = 'Spell';
```

## Claude Code tools (MCP server)

`mcp_server/` is a local MCP server that lets Claude Code search the cards and build, analyze and
save decks. It runs as a child process of Claude Code over stdin/stdout, so there's no port, no
hosting and nothing to install (standard-library Python).

**Setup:** open Claude Code in this folder (`claude`). It reads `.mcp.json` and asks you once to
approve the `duelmasters` server. Then ask for a deck in plain English:

> Build an aggressive Fire/Nature rush deck with Speed Attackers, using only DM-01 to DM-12.
>
> Make a Water/Darkness control deck with lots of removal and card draw.
>
> Look at my deck `armored-dragon-overlords` and suggest improvements against rush decks.

You can also run the prompt directly: `/mcp__duelmasters__build_deck`.

| Tool | Purpose |
|---|---|
| `search_cards` | Filter by civilization, cost, type, race, keyword, effect category, set, power, name or rules text |
| `get_cards` | Full card details (abilities, tags, evolution requirement, image path) |
| `list_values` | Valid races, keywords, categories, sets, etc. |
| `find_synergies` | Evolution bases, evolutions a card can become, race support, cards that mention it by name |
| `analyze_deck` | Mana curve, civilization split, role counts, evolution base check, warnings |
| `validate_deck` | 40 cards, at most 4 copies per name, all cards exist |
| `save_deck` / `list_decks` / `get_deck` / `delete_deck` | Decks stored as JSON in `data/decks/` |
| `deckbuilding_guide` | Rules and archetype guidelines Claude follows (`mcp_server/deckbuilding_guide.md`) |

Saved decks look like this, ready for a game to load:

```json
{
  "slug": "water-darkness-control",
  "name": "Water-Darkness Control",
  "strategy": "...",
  "valid": true,
  "total_cards": 40,
  "civilizations": ["Water", "Darkness"],
  "cards": [{"id": "2216", "name": "Cosmo Politan", "count": 4, "image_path": "data/images/2216.webp"}]
}
```

`data/decks/` includes three example decks built by Claude during testing.

The deck logic lives in `mcp_server/dmdb.py` with no MCP code, so a game can import it directly.

**Tests:** `python -m unittest discover tests` · **Server self-test:** `python mcp_server/server.py --selftest`

**Limits:** the database has no ban list or format data, so only the basic deck rules are checked.
The tools can't simulate games, so judgments about deck strength are Claude's opinion.

## Note on usage

The card art and text belong to Wizards of the Coast / Takara Tomy. Personal or private use is
generally tolerated, but get permission (or use your own art) before you publish or distribute a
game that includes them.
