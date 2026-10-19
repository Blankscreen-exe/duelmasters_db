# Duel Masters deckbuilding guide

## Hard rules (checked by `validate_deck`)

- Exactly **40 cards**.
- At most **4 copies** of any card with the same name. Reprints share a name, so they share the limit.

## Game basics that shape a deck

- Each player starts with 5 shields from the top of the deck and draws 5 cards.
- Each turn you may put one card from your hand into your mana zone. To play a card you tap
  mana equal to its cost, and at least one of those mana must match **each** of the card's civilizations.
- Multicolor cards enter the mana zone **tapped**, so too many of them slow your early turns.
- Creatures can't attack the turn they're summoned, unless they have **Speed Attacker**.
  **Evolution creatures** go on top of a matching creature you already have and can attack right away.
- Attacking breaks shields, and broken shields go to their owner's hand. A **Shield Trigger** card
  can be played for free when its shield is broken, which is the main defense against rush decks.
- When a player has no shields left, the next attack that gets through wins the game.

## Building process

1. **Pin down the request.** What are the civilizations, the plan (rush, midrange, control, tribal…),
   any set or card restrictions, and the key cards the user mentioned?
2. **Pick the core:** 2-4 cards or a race that define the deck. Use `find_synergies` on them to see
   evolution bases, race support and evolutions.
3. **Fill the roles** with `search_cards`, filtered by civilization plus keyword or category:
   - early plays (cost 2-3) to develop the board or defend
   - removal (`categories: ["removal"]`), card draw (`draw`) and ramp (`ramp`, mostly Nature)
   - Shield Triggers (`keywords: ["shield_trigger"]`)
   - finishers: Double Breakers or better, or strong evolutions
4. **Assemble 40 cards**, then run `analyze_deck` and read the warnings and stats.
5. **Fix the weak spots** (curve, trigger count, mana balance, evolution bases) and analyze again.
6. **Validate and save** with `save_deck`, including a short strategy description.
7. **Explain the deck to the user:** the game plan, key cards and why, and the weaknesses.

## Guidelines (adjust to the archetype)

| | Rush / aggro | Midrange | Control |
|---|---|---|---|
| Cards costing 3 or less | 16-22 | 10-16 | 8-12 |
| Most common costs | 2-4 | 3-5 | 4-7 |
| Shield Triggers | 4-8 | 6-10 | 8-14 |
| Focus | Speed Attackers, cheap breakers | strong creatures, removal | removal, draw, blockers, a few big finishers |

- **Civilizations:** 2 civilizations is the most consistent. 3 works with ramp or multicolor support;
  4 or more is rarely worth it. Give each civilization enough cards to reliably show up in mana,
  roughly 10 or more each in a 2-civilization deck.
- **Copies:** run 4 of the cards your plan depends on, and 2-3 of situational cards. Avoid many 1-ofs,
  since they make the deck inconsistent.
- **Evolutions:** include at least about 2 matching base creatures per evolution card, and favor cheap bases.
- **Creature count:** most decks want 14 or more creatures to break shields. Spell-heavy control decks can go lower.
- **Cross Gear and Castles** (later sets) support creatures and shields; count them as support, not threats.

## Honesty rules

- Use only cards returned by the tools; never invent cards or card text.
- Base claims about a card on its actual rules text (`get_cards`).
- The tools can't simulate games. Present strength assessments as judgment, not fact.
- There's no ban list or format data. Mention this if the user asks about tournament legality.
