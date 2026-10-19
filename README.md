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
| `data/cards.json` | Same data as one JSON array (easy to load into Unity/Godot/JS) |

## Schema

- **cards**: one row per card: `id`, `name`, `card_type`, `cost`, `power`, `civilizations`, `races`,
  `rarity`, `set_code`, `collector_number`, `rules_text`, `image_path`, `video_url`, `wiki_url`
- **card_abilities**: each rules-text line in order (`text`, `reminder`, `keyword_icon`)
- **card_civilizations** / **card_races**: one row per civ/race (for multi-civ and multi-race cards)
- **keywords** + **card_keywords**: Blocker, Shield Trigger, Double Breaker, Speed Attacker, ...
- **categories** + **card_categories**: effect tags such as removal, draw, ramp, triggers, ...
- **sets**

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

## Note on usage

The card art and text belong to Wizards of the Coast / Takara Tomy. Personal or private use is
generally tolerated, but get permission (or use your own art) before you publish or distribute a
game that includes them.
