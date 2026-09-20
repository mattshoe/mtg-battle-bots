"""Decks in, Forge ``.dck`` files out.

Two sources feed this: a plain text decklist, which is all the harness needs,
and a sharded SQLite collection database, which is a convenience for one
particular setup. Both land on :class:`DeckList`, and only :func:`to_dck` knows
what Forge wants to read.

The collection database is opened read-only and never written to. It is a
record of cards someone physically owns, and a harness that plays games has no
business changing it.
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

# decks and deck_cards live in the core shard. The other shards hold oracle text,
# legality and tags, none of which a .dck needs.
CORE_DB = "collection-core.db"

#: Where the collection database lives, if there is one.
#:
#: Set GAUNTLET_COLLECTION to point at a directory holding the sharded SQLite
#: collection. Without it, the only deck source is a text decklist, which is all
#: this harness needs to run. The collection reader is a convenience for one
#: particular setup, not a dependency.
#:
#: The fallback searches Google Drive rather than naming an account, because
#: hardcoding one person's path into a shared tool helps exactly one person.
_COLLECTION_ENV = "GAUNTLET_COLLECTION"


def _default_db_dir() -> Path | None:
    raw = os.environ.get(_COLLECTION_ENV)
    if raw:
        return Path(raw).expanduser()
    drive = Path.home() / "Library/CloudStorage"
    if drive.is_dir():
        for account in sorted(drive.glob("GoogleDrive-*")):
            candidate = account / "My Drive/claude-sandbox/mtg"
            if (candidate / CORE_DB).exists():
                return candidate
    return None


# Singleton rules exempt these, so more than one copy is not a deckbuilding error.
BASIC_LANDS = frozenset(
    {
        "plains",
        "island",
        "swamp",
        "mountain",
        "forest",
        "wastes",
        "snow-covered plains",
        "snow-covered island",
        "snow-covered swamp",
        "snow-covered mountain",
        "snow-covered forest",
    }
)

COMMANDER_DECK_SIZE = 100


class DeckError(Exception):
    """A deck that cannot be turned into something Forge will load."""


@dataclass(frozen=True, slots=True)
class DeckRow:
    """One row of the decks table, enough to pick a deck by."""

    slug: str
    name: str
    owner: str
    commander: str
    card_count: int


@dataclass(frozen=True, slots=True)
class DeckList:
    """A deck as Forge needs to see it: commanders apart from everything else."""

    name: str
    commanders: tuple[str, ...]
    main: tuple[tuple[int, str], ...]
    source: str

    @property
    def size(self) -> int:
        return sum(qty for qty, _ in self.main) + len(self.commanders)


# --------------------------------------------------------------------------- #
# name handling
# --------------------------------------------------------------------------- #


def _norm(name: str) -> str:
    """Match key for card names.

    Accents and curly apostrophes differ between the database, Scryfall exports
    and hand-typed lists, so they are folded away before comparing. Clavileño
    and Yuna's Guardian both arrive spelled more than one way.
    """
    folded = unicodedata.normalize("NFKD", name.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'"))
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


# Forge keeps both halves in a card's name for these and uses only the front
# face for everything else, adventures and modal lands and transforming cards
# included. Its own precon files show the difference: "Dusk // Dawn" but plain
# "Brazen Borrower". Scryfall's layout is what tells the two groups apart.
_TWO_HALF_NAME_LAYOUTS = frozenset({"split", "room"})


def _forge_name(name: str, layout: str | None = None) -> str:
    """Spell a card the way Forge's card database spells it.

    Some commanders come out of a collection as ``X // X``, because the
    printing is an art card or a reversible card and the importer took the name
    off the printing. Jetmir and Breya and Dina are ordinary one-faced cards, so
    the doubled name matches nothing in Forge.

    With no layout to go on the name is left exactly as it came in. Guessing
    would break split cards and adventures in opposite directions, and a wrong
    guess is a deck that will not load.
    """
    if " // " not in name:
        return name
    front, _, back = name.partition(" // ")
    if front == back:
        return front
    if layout is None or layout in _TWO_HALF_NAME_LAYOUTS:
        return name
    return front


def _split_partners(base: str, known: dict[str, str]) -> list[str]:
    """Split a two-commander string, but only when the halves are real cards.

    ``A // B`` and ``A and B`` are how partner pairs are written. The catch is
    that "Gisa and Geralf" and "Shiko and Narset, Unified" are single cards, so
    splitting on the word blindly invents two commanders that do not exist. The
    full string wins whenever the deck contains a card by that name, and a split
    only happens when both halves are cards and the whole is not.
    """
    if _norm(base) in known:
        return [base]
    for sep in (" // ", " and "):
        if sep not in base:
            continue
        halves = [h.strip() for h in base.split(sep)]
        if len(halves) == 2 and all(_norm(h) in known for h in halves):
            return halves
    return [base]


def parse_commander_field(raw: str, known: dict[str, str] | None = None) -> tuple[str, ...]:
    """Pull commander names out of the free text in ``decks.commander``.

    Every row in the real database carries an aside in parentheses, usually the
    alternate commander the precon ships with, sometimes the name a proxy was
    printed under. The commander is everything before the first ``(``.

    ``known`` maps normalized name to the exact spelling in ``deck_cards``. When
    a name is in there the deck's own spelling wins, since that is what the rest
    of the export uses.
    """
    known = known or {}
    base = raw.split(" (", 1)[0].strip()
    if not base:
        raise DeckError(f"no commander name in {raw!r}")

    resolved: list[str] = []
    for candidate in _split_partners(base, known):
        exact = known.get(_norm(candidate))
        if exact is None and known:
            raise DeckError(
                f"commander {candidate!r} (from {raw!r}) is not a card in this deck's list"
            )
        resolved.append(exact or candidate)
    return tuple(resolved)


# --------------------------------------------------------------------------- #
# collection database
# --------------------------------------------------------------------------- #


def _connect(db_dir: Path | None) -> sqlite3.Connection:
    base = db_dir or _default_db_dir()
    if base is None:
        raise DeckError(
            "no collection database found. Set "
            f"{_COLLECTION_ENV} to the directory holding {CORE_DB}, or pass a "
            "decklist file instead of a collection slug."
        )
    path = base / CORE_DB
    if not path.exists():
        raise DeckError(f"collection database not found at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_collection_decks(*, owner: str | None = None, db_dir: Path | None = None) -> list[DeckRow]:
    """Every deck in the collection, newest naming conventions and all."""
    sql = "SELECT slug, name, owner, commander, card_count FROM decks"
    params: list[str] = []
    if owner:
        sql += " WHERE owner = ?"
        params.append(owner)
    sql += " ORDER BY owner, slug"

    with closing(_connect(db_dir)) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        DeckRow(
            slug=r["slug"] or "",
            name=r["name"] or "",
            owner=r["owner"] or "",
            commander=r["commander"] or "",
            card_count=r["card_count"] or 0,
        )
        for r in rows
    ]


def _layouts(conn: sqlite3.Connection, names: list[str]) -> dict[str, str]:
    """Scryfall layout for the two-part names in a deck, keyed by match key.

    Only these names need it, and a card the collection does not hold simply has
    no layout, which :func:`_forge_name` treats as unknown.
    """
    wanted = [n for n in names if " // " in n]
    if not wanted:
        return {}
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT name, layout FROM cards WHERE name IN ({placeholders}) AND layout IS NOT NULL",
        wanted,
    ).fetchall()
    return {_norm(r["name"]): r["layout"] for r in rows}


def from_collection(
    slug_or_name: str, *, owner: str | None = None, db_dir: Path | None = None
) -> DeckList:
    """Load one deck out of the collection database."""
    sql = """
        SELECT id, slug, name, owner, commander
        FROM decks
        WHERE (slug = :key COLLATE NOCASE OR name = :key COLLATE NOCASE)
    """
    params: dict[str, str] = {"key": slug_or_name}
    if owner:
        sql += " AND owner = :owner"
        params["owner"] = owner

    with closing(_connect(db_dir)) as conn:
        matches = conn.execute(sql, params).fetchall()
        if not matches:
            raise DeckError(f"no deck matching {slug_or_name!r}")
        if len(matches) > 1:
            found = ", ".join(f"{m['slug']} ({m['owner']})" for m in matches)
            raise DeckError(f"{slug_or_name!r} matches more than one deck: {found}")
        deck = matches[0]

        # deck_cards already dedupes by name, the eight Swamps are one row with
        # qty 8. Counting rows would report a 100 card deck as 70-odd cards.
        cards = conn.execute(
            """
            SELECT name, SUM(qty) AS qty
            FROM deck_cards
            WHERE deck_id = ?
            GROUP BY name_norm
            """,
            (deck["id"],),
        ).fetchall()

        layouts = _layouts(conn, [row["name"] or "" for row in cards])

    if not cards:
        raise DeckError(f"deck {deck['slug']!r} has no cards")

    named = []
    for row in cards:
        raw = row["name"] or ""
        named.append((int(row["qty"] or 0), _forge_name(raw, layouts.get(_norm(raw)))))

    known = {_norm(name): name for _, name in named}
    for row in cards:
        known.setdefault(_norm(row["name"] or ""), _forge_name(row["name"] or ""))

    commanders = parse_commander_field(deck["commander"] or "", known)
    commander_keys = {_norm(c) for c in commanders}

    # Forge refuses a deck whose commander is also one of the 99, and the
    # database stores the commander as an ordinary deck_cards row. Drop it here
    # rather than making every caller remember to.
    main = tuple((qty, name) for qty, name in named if _norm(name) not in commander_keys)

    return DeckList(
        name=deck["name"] or deck["slug"] or slug_or_name,
        commanders=commanders,
        main=_sorted_main(main),
        source=f"collection:{deck['slug']}",
    )


# --------------------------------------------------------------------------- #
# text decklists
# --------------------------------------------------------------------------- #

_SECTION = re.compile(
    r"^\s*[\[#*]*\s*"
    r"(commanders?|main|maindeck|mainboard|deck|sideboard|companion|lands?|creatures?|spells?)"
    r"\s*[\]:]*\s*(?:\([^)]*\))?\s*$",
    re.IGNORECASE,
)
_CARD = re.compile(r"^\s*(?:(\d+)\s*[xX]?\s+)?(.+?)\s*$")

# Moxfield and Archidekt append the printing, "1 Opt (M21) 59". The set is
# dropped for the same reason to_dck omits it.
_PRINTING = re.compile(r"\s*\([0-9A-Z]{2,6}\)(?:\s+[\dA-Za-z\-★]+)?\s*$")

_SKIP_SECTIONS = frozenset({"sideboard", "companion"})


def from_text(text: str, name: str) -> DeckList:
    """Parse a pasted decklist.

    Accepts the shapes people actually paste: an optional ``Commander:`` header,
    ``N Card Name`` lines, bare names meaning one copy, and blank or ``#``
    commented lines.
    """
    commanders: list[str] = []
    counts: dict[str, int] = {}
    order: dict[str, str] = {}
    section = "main"

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue

        header = _SECTION.match(line)
        if header:
            word = header.group(1).lower()
            section = "commander" if word.startswith("commander") else word
            continue

        if section in _SKIP_SECTIONS:
            continue

        card = _CARD.match(line)
        if not card:
            continue
        qty = int(card.group(1) or 1)
        card_name = _PRINTING.sub("", card.group(2)).strip()
        if not card_name:
            continue

        if section == "commander":
            commanders.extend([card_name] * max(qty, 1))
            continue

        key = _norm(card_name)
        counts[key] = counts.get(key, 0) + qty
        order.setdefault(key, card_name)

    commander_keys = {_norm(c) for c in commanders}
    main = tuple((qty, order[key]) for key, qty in counts.items() if key not in commander_keys)
    return DeckList(
        name=name,
        commanders=tuple(commanders),
        main=_sorted_main(main),
        source="text",
    )


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _sorted_main(main: tuple[tuple[int, str], ...]) -> tuple[tuple[int, str], ...]:
    # Sorted so exporting the same deck twice gives byte-identical files and a
    # diff between two runs means the deck really changed.
    return tuple(sorted(main, key=lambda item: (item[1].casefold(), item[1])))


def to_dck(deck: DeckList) -> str:
    """Render the Forge ``.dck`` text.

    Card lines may carry a ``|SETCODE`` suffix and Forge's own precons do, but
    this omits it. The collection records the printing Matt owns, and Forge only
    knows the sets its card data was built with. Pinning one to the other turns a
    missing set into a deck that will not load, where leaving it off just lets
    Forge pick whatever printing it has.
    """
    lines = ["[metadata]", f"Name={deck.name}", "[Commander]"]
    lines += [f"1 {c}" for c in deck.commanders]
    lines.append("[Main]")
    lines += [f"{qty} {card}" for qty, card in deck.main]
    return "\n".join(lines) + "\n"


def write_dck(deck: DeckList, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Forge reads these as UTF-8 with unix line endings, and several commanders
    # have accented names.
    path.write_text(to_dck(deck), encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


#: Cards whose own text overrides the singleton rule say so in the same words
#: every time, which is what makes checking oracle text reliable rather than a
#: guess. Relentless Rats, Persistent Petitioners, Tempest Hawk and the rest.
_UNLIMITED_PHRASE = "any number of cards named"


def _unlimited_copy_cards(names: Sequence[str], *, db_dir: Path | None = None) -> set[str]:
    """Which of these cards let a deck run as many copies as it likes.

    Looked up rather than listed, because the list grows every set and a stale
    one reports a legal deck as illegal. A collection that cannot be read means
    no exemptions, which is the safe direction: a false warning, not a false
    clean bill.
    """
    if not names:
        return set()
    try:
        with closing(_connect(db_dir)) as conn:
            placeholders = ",".join("?" * len(names))
            rows = conn.execute(
                f"SELECT DISTINCT name FROM cards "
                f"WHERE name IN ({placeholders}) AND oracle_text LIKE ?",
                (*names, f"%{_UNLIMITED_PHRASE}%"),
            ).fetchall()
    except (sqlite3.Error, DeckError, OSError):
        return set()
    return {_norm(row[0]) for row in rows}


def validate(deck: DeckList) -> list[str]:
    """List what is wrong with a deck, in words.

    Nothing here raises. A deck can be worth playing while still being illegal,
    and the caller is the one who knows whether this run cares.
    """
    problems: list[str] = []

    if not deck.commanders:
        problems.append("no commander")

    for qty, card in deck.main:
        if qty < 1:
            problems.append(f"{card}: quantity {qty} is not a real number of copies")

    if deck.size != COMMANDER_DECK_SIZE:
        problems.append(
            f"{deck.size} cards, Commander wants {COMMANDER_DECK_SIZE} "
            f"({len(deck.commanders)} commander plus {deck.size - len(deck.commanders)})"
        )

    unlimited = _unlimited_copy_cards([card for qty, card in deck.main if qty > 1])
    for qty, card in deck.main:
        if qty > 1 and _norm(card) not in BASIC_LANDS and _norm(card) not in unlimited:
            problems.append(f"{card}: {qty} copies of a nonbasic in a singleton deck")

    seen: dict[str, str] = {}
    for _, card in deck.main:
        key = _norm(card)
        if key in seen:
            problems.append(f"{card}: listed twice in the main deck")
        seen[key] = card

    commander_keys = {_norm(c) for c in deck.commanders}
    for _, card in deck.main:
        if _norm(card) in commander_keys:
            problems.append(f"{card}: commander is also in the main deck, Forge will refuse it")

    return problems
