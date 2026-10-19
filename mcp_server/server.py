"""
Duel Masters MCP server (stdio transport, standard library only).

Claude Code launches this as a child process (see .mcp.json) and talks to it with
newline-delimited JSON-RPC 2.0 over stdin/stdout. Nothing listens on a port.

Run the self-test:  python mcp_server/server.py --selftest
"""

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dmdb import CardDB, DMError  # noqa: E402

SERVER_NAME = "duelmasters"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
GUIDE_PATH = Path(__file__).resolve().parent / "deckbuilding_guide.md"

INSTRUCTIONS = """\
Tools for the local Duel Masters card database (2,600+ cards, DM-01 to DM-35 plus promos).
When building or improving a deck: call deckbuilding_guide first, use search_cards / find_synergies
to pick real cards, run analyze_deck and fix its warnings, then save_deck (which also writes
an HTML page in decks/; tell the user its path). Only use cards returned
by these tools. Cards can be referenced by id (e.g. "0079") or exact name. Filter values for
keywords and categories are codes (e.g. "shield_trigger", "removal"); use list_values to look them up."""

DECK_CARDS_SCHEMA = {
    "type": "array",
    "description": "Deck list: one entry per card with a count. Duplicate names are merged.",
    "items": {
        "type": "object",
        "properties": {
            "card": {"type": "string", "description": "Card id (e.g. '0079') or exact card name."},
            "count": {"type": "integer", "minimum": 1, "description": "Number of copies (max 4 per name)."},
        },
        "required": ["card", "count"],
    },
}

STR_ARRAY = {"type": "array", "items": {"type": "string"}}
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}

TOOLS = [
    {
        "name": "search_cards",
        "description": "Search the card database with filters. All filters are optional and combined with AND. "
                       "Returns compact card summaries (reminder text stripped), one printing per name by default.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Substring of the card name."},
                "text": {"type": "string", "description": "Substring of the rules text, e.g. 'draw a card'."},
                "civilizations": {**STR_ARRAY, "description": "Light, Water, Darkness, Fire, Nature."},
                "civ_match": {"type": "string", "enum": ["any", "all", "exact"],
                              "description": "any = has at least one of the civs (default); all = has every "
                                             "listed civ; exact = has exactly these civs (use for mono-color)."},
                "card_types": {**STR_ARRAY, "description": "Creature, Evolution Creature, Spell, Cross Gear, "
                                                           "Evolution Cross Gear, Castle."},
                "races": {**STR_ARRAY, "description": "Race names or parts, e.g. 'Armored Dragon' or 'Dragon' "
                                                      "(matches any race containing it). Matches any listed."},
                "keywords": {**STR_ARRAY, "description": "Keyword codes, all required, e.g. 'blocker', "
                                                         "'shield_trigger', 'speed_attack', 'double_break'."},
                "categories": {**STR_ARRAY, "description": "Effect category codes, all required, e.g. 'removal', "
                                                           "'draw', 'ramp', 'trigger_etb'."},
                "cost_min": {"type": "integer"},
                "cost_max": {"type": "integer"},
                "power_min": {"type": "integer"},
                "power_max": {"type": "integer"},
                "sets": {**STR_ARRAY, "description": "Set codes, e.g. 'DM-01', 'PROMO'."},
                "rarity": {"type": "string", "description": "c, u, r, vr, sr or the full name."},
                "include_reprints": {"type": "boolean", "description": "Return every printing (default false)."},
                "sort": {"type": "string", "enum": ["cost", "power", "name", "id"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Default 25."},
                "offset": {"type": "integer", "minimum": 0},
            },
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "get_cards",
        "description": "Full details for up to 60 cards: all ability text, keywords, effect categories, "
                       "evolution requirement, reprints and image path.",
        "inputSchema": {
            "type": "object",
            "properties": {"cards": {**STR_ARRAY, "description": "Card ids or exact names."}},
            "required": ["cards"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "list_values",
        "description": "List valid filter values with card counts: civilizations, card_types, races, "
                       "keywords, categories, sets or rarities.",
        "inputSchema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["civilizations", "card_types", "races", "keywords",
                                                               "categories", "sets", "rarities"]}},
            "required": ["kind"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "find_synergies",
        "description": "Find cards that work with a given card: valid evolution bases (for evolutions), "
                       "evolutions it can be a base for, cards whose text supports its race, and cards that "
                       "mention it by name. Results favor cards sharing its civilization.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "card": {"type": "string", "description": "Card id or exact name."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Per group, default 15."},
            },
            "required": ["card"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "analyze_deck",
        "description": "Analyze a deck list: validation, mana curve, civilization split, card types, role "
                       "counts (shield triggers, blockers, removal, draw, ramp, breakers), evolution base "
                       "check, and warnings about common weaknesses.",
        "inputSchema": {"type": "object", "properties": {"cards": DECK_CARDS_SCHEMA}, "required": ["cards"]},
        "annotations": READ_ONLY,
    },
    {
        "name": "validate_deck",
        "description": "Check the hard deck rules: 40-50 cards (40 is ideal), at most 4 copies per card name, all cards exist.",
        "inputSchema": {"type": "object", "properties": {"cards": DECK_CARDS_SCHEMA}, "required": ["cards"]},
        "annotations": READ_ONLY,
    },
    {
        "name": "save_deck",
        "description": "Save a deck to data/decks/<slug>.json (card ids, counts, image paths, strategy) "
                       "and write its HTML page to decks/<slug>.html. "
                       "Refuses invalid decks unless allow_invalid is true, and won't replace an existing "
                       "deck unless overwrite is true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Deck name; also used for the file name."},
                "cards": DECK_CARDS_SCHEMA,
                "strategy": {"type": "string", "description": "How the deck plans to win, key cards, mulligan/play tips."},
                "notes": {"type": "string", "description": "Anything else: the user's request, weaknesses, swap options."},
                "overwrite": {"type": "boolean"},
                "allow_invalid": {"type": "boolean", "description": "Save even if the deck breaks the rules (drafts)."},
            },
            "required": ["name", "cards"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "list_decks",
        "description": "List saved decks.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": READ_ONLY,
    },
    {
        "name": "get_deck",
        "description": "Load a saved deck by its slug (from list_decks).",
        "inputSchema": {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]},
        "annotations": READ_ONLY,
    },
    {
        "name": "delete_deck",
        "description": "Delete a saved deck file. Only use when the user asks for it.",
        "inputSchema": {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    },
    {
        "name": "export_deck_html",
        "description": "Write decks/<slug>.html for a saved deck: a grid of card images with each name in a "
                       "copyable box. save_deck already does this automatically; use this to refresh an "
                       "existing deck's page. Returns the file path for the user to open in a browser.",
        "inputSchema": {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "open_deck_listing",
        "description": "Rebuild decks/index.html (a page listing every deck in the decks folder, each linking "
                       "to its deck page) and open it in the user's default browser.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "deckbuilding_guide",
        "description": "Duel Masters deck rules, building process and archetype guidelines. "
                       "Read this before building or reviewing a deck.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": READ_ONLY,
    },
]

PROMPTS = [
    {
        "name": "build_deck",
        "description": "Build a Duel Masters deck from a description, then analyze, validate and save it.",
        # Optional: Claude Code splits slash-command text on spaces, so a multi-word request only
        # arrives intact when quoted. Without it, Claude asks the user instead.
        "arguments": [{"name": "request", "description": "What kind of deck you want (wrap in quotes).",
                       "required": False}],
    },
]


class Server:
    def __init__(self):
        self.db = CardDB()
        self.handlers = {
            "search_cards": lambda a: self.db.search(**a),
            "get_cards": lambda a: self.db.get_cards(a.get("cards")),
            "list_values": lambda a: self.db.list_values(a.get("kind")),
            "find_synergies": lambda a: self.db.find_synergies(**a),
            "analyze_deck": lambda a: self.db.analyze_deck(a.get("cards")),
            "validate_deck": lambda a: self.db.validate_deck(a.get("cards")),
            "save_deck": lambda a: self.db.save_deck(**a),
            "list_decks": lambda a: self.db.list_decks(),
            "get_deck": lambda a: self.db.get_deck(a.get("slug")),
            "delete_deck": lambda a: self.db.delete_deck(a.get("slug")),
            "export_deck_html": lambda a: self.db.export_deck_html(a.get("slug")),
            "open_deck_listing": lambda a: self.db.open_deck_listing(),
            "deckbuilding_guide": lambda a: GUIDE_PATH.read_text(encoding="utf-8"),
        }

    # ------------------------------------------------------------- JSON-RPC

    def handle(self, msg):
        """Return a response dict, or None for notifications."""
        method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if msg_id is None:  # notification (e.g. notifications/initialized) - no reply
            return None
        try:
            if method == "initialize":
                result = self.initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.call_tool(params)
            elif method == "prompts/list":
                result = {"prompts": PROMPTS}
            elif method == "prompts/get":
                result = self.get_prompt(params)
            else:
                return _error(msg_id, -32601, f"Method not found: {method}")
        except RpcError as e:
            return _error(msg_id, e.code, str(e))
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def initialize(self, params):
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }

    def call_tool(self, params):
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in self.handlers:
            raise RpcError(-32602, f"Unknown tool: {name}")
        if not isinstance(args, dict):
            raise RpcError(-32602, "arguments must be an object")
        try:
            result = self.handlers[name](args)
        except DMError as e:
            return _tool_text(str(e), is_error=True)
        except TypeError as e:  # unexpected/missing argument names
            return _tool_text(f"Invalid arguments for {name}: {e}", is_error=True)
        except Exception as e:  # keep the server alive; report the failure to the model
            traceback.print_exc(file=sys.stderr)
            return _tool_text(f"Internal error in {name}: {e}", is_error=True)
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return _tool_text(text)

    def get_prompt(self, params):
        if params.get("name") != "build_deck":
            raise RpcError(-32602, f"Unknown prompt: {params.get('name')}")
        request = ((params.get("arguments") or {}).get("request") or "").strip()
        guide = GUIDE_PATH.read_text(encoding="utf-8")
        if request:
            ask = (f"Build a Duel Masters deck for this request:\n\n{request}\n\n"
                   f"If the request looks cut off or too vague to act on, ask the user what they want first.")
        else:
            ask = "Build a Duel Masters deck. First ask the user what kind of deck they want."
        text = (f"{ask}\n\nFollow the guide below. Use the duelmasters tools for every card choice, run "
                f"analyze_deck and fix its warnings, validate, save with save_deck, then explain the deck.\n\n{guide}")
        return {"description": "Build a Duel Masters deck",
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_text(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def serve():
    stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", newline="\n", closefd=False)
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False)
    try:
        server = Server()
    except DMError as e:
        print(f"[duelmasters] {e}", file=sys.stderr)
        sys.exit(1)
    print("[duelmasters] ready", file=sys.stderr, flush=True)

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = _error(None, -32700, "Parse error")
        else:
            if isinstance(msg, list):  # legacy JSON-RPC batch
                replies = [r for r in (server.handle(m) for m in msg if isinstance(m, dict)) if r]
                reply = replies or None
            elif isinstance(msg, dict):
                reply = server.handle(msg)
            else:
                reply = _error(None, -32600, "Invalid request")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def selftest():
    """Drive the server in-process the way a client would."""
    s = Server()
    calls = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "selftest"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "search_cards", "arguments": {"civilizations": ["Fire"], "keywords": ["speed_attack"],
                                                         "cost_max": 3, "limit": 3}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "get_cards", "arguments": {"cards": ["Bolshack Dragon"]}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "search_cards", "arguments": {"keywords": ["not_a_keyword"]}}},
        {"jsonrpc": "2.0", "id": 6, "method": "prompts/get",
         "params": {"name": "build_deck", "arguments": {"request": "fire rush"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "bogus/method"},
    ]
    for c in calls:
        r = s.handle(c)
        if r is None:
            print(f"-> {c['method']}: (notification, no reply)")
            continue
        if "error" in r:
            print(f"-> {c['method']}: ERROR {r['error']}")
        elif c["method"] == "tools/list":
            print(f"-> tools/list: {[t['name'] for t in r['result']['tools']]}")
        elif c["method"] == "tools/call":
            res = r["result"]
            print(f"-> tools/call {c['params']['name']} (isError={res['isError']}): "
                  f"{res['content'][0]['text'][:300]}")
        else:
            print(f"-> {c['method']}: {json.dumps(r['result'], ensure_ascii=False)[:200]}")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.stdout.reconfigure(encoding="utf-8")
        selftest()
    else:
        serve()
