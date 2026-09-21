"""Tests for the deck reader and the Forge exporter.

The collection tests are skipped when the database is not mounted, so this suite
passes on a machine that has never seen Matt's Google Drive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gauntlet.decks import (
    CORE_DB,
    DeckError,
    DeckList,
    _default_db_dir,
    from_collection,
    from_dck,
    from_text,
    list_collection_decks,
    parse_commander_field,
    to_dck,
    validate,
    write_dck,
)


# Computed lazily, because conftest redirects the collection to a committed
# fixture for the whole suite and this used to be evaluated at import time,
# before that happened.
def _have_real_db() -> bool:
    import os

    saved = os.environ.pop("GAUNTLET_COLLECTION", None)
    try:
        found = _default_db_dir()
        return found is not None and (found / CORE_DB).exists()
    finally:
        if saved is not None:
            os.environ["GAUNTLET_COLLECTION"] = saved


HAVE_DB = _have_real_db()
needs_db = pytest.mark.skipif(not HAVE_DB, reason="collection database is not mounted")

FORGE_PRECON = (
    Path(__file__).resolve().parents[1]
    / "vendor/res/quest/commanderprecons/Silverquill Influence [SOC] [2026].dck"
)

SILVERQUILL_TEXT = """
Commander
1 Killian, Decisive Mentor

Main
8 Swamp
8 Plains
1 War Room
"""


def commander_deck(commanders=("Killian, Decisive Mentor",), main_size=99) -> DeckList:
    """A legal-sized deck, padded with basics so validate() has nothing to say."""
    return DeckList(
        name="Test",
        commanders=commanders,
        main=((main_size, "Swamp"),),
        source="test",
    )


# --------------------------------------------------------------------------- #
# text parsing
# --------------------------------------------------------------------------- #


def test_from_text_reads_sections_and_quantities():
    deck = from_text(SILVERQUILL_TEXT, "Silverquill Influence")
    assert deck.name == "Silverquill Influence"
    assert deck.commanders == ("Killian, Decisive Mentor",)
    assert deck.main == ((8, "Plains"), (8, "Swamp"), (1, "War Room"))
    assert deck.size == 18
    assert deck.source == "text"


def test_from_text_defaults_missing_quantity_to_one():
    deck = from_text("Sol Ring\n2 Forest", "x")
    assert deck.main == ((2, "Forest"), (1, "Sol Ring"))


def test_from_text_ignores_comments_blank_lines_and_sideboard():
    text = """
    # shopping list
    // not a card
    1 Sol Ring

    Sideboard
    1 Pithing Needle
    """
    assert from_text(text, "x").main == ((1, "Sol Ring"),)


def test_from_text_strips_moxfield_printing_suffix():
    deck = from_text("1 Opt (M21) 59\n1 Brazen Borrower // Petty Theft (ELD) 39", "x")
    assert deck.main == ((1, "Brazen Borrower // Petty Theft"), (1, "Opt"))


def test_from_text_merges_repeated_lines():
    assert from_text("1 Forest\n3 Forest", "x").main == ((4, "Forest"),)


def test_from_text_sorts_main_case_insensitively():
    deck = from_text("1 zealot\n1 Abbey\n1 Mox", "x")
    assert [name for _, name in deck.main] == ["Abbey", "Mox", "zealot"]


# --------------------------------------------------------------------------- #
# commander handling
# --------------------------------------------------------------------------- #


def test_commander_is_filtered_out_of_main():
    text = """
    Commander
    1 Killian, Decisive Mentor
    Main
    1 Killian, Decisive Mentor
    1 War Room
    """
    deck = from_text(text, "x")
    assert deck.main == ((1, "War Room"),)
    assert deck.commanders == ("Killian, Decisive Mentor",)


def test_commander_filter_ignores_accents_and_curly_apostrophes():
    text = "Commander\n1 Clavileno, First of the Blessed\nMain\n1 Clavileño, First of the Blessed"
    assert from_text(text, "x").main == ()


def test_parse_commander_strips_featured_alt_aside():
    raw = "Killian, Decisive Mentor (featured alt commander: Scriv, the Obligator)"
    assert parse_commander_field(raw) == ("Killian, Decisive Mentor",)


def test_parse_commander_strips_proxy_aside():
    raw = 'Astor, Bearer of Blades (proxied as "Master Chief, Spartan Hero")'
    assert parse_commander_field(raw) == ("Astor, Bearer of Blades",)


def test_parse_commander_strips_multi_commander_aside():
    raw = "Galadriel, Elven-Queen (deck supports 5 other viable commanders in the 99: Elrond)"
    assert parse_commander_field(raw) == ("Galadriel, Elven-Queen",)


def test_parse_commander_splits_slash_partners():
    known = {"tana, the bloodsower": "Tana, the Bloodsower", "tymna the weaver": "Tymna the Weaver"}
    raw = "Tana, the Bloodsower // Tymna the Weaver (partners)"
    assert parse_commander_field(raw, known) == ("Tana, the Bloodsower", "Tymna the Weaver")


def test_parse_commander_splits_and_partners():
    known = {"tana, the bloodsower": "Tana, the Bloodsower", "tymna the weaver": "Tymna the Weaver"}
    raw = "Tana, the Bloodsower and Tymna the Weaver"
    assert parse_commander_field(raw, known) == ("Tana, the Bloodsower", "Tymna the Weaver")


def test_parse_commander_keeps_and_when_it_is_one_card():
    # "Gisa and Geralf" and "Shiko and Narset, Unified" are single cards, and both
    # are real commanders in the collection.
    known = {"gisa and geralf": "Gisa and Geralf"}
    assert parse_commander_field("Gisa and Geralf", known) == ("Gisa and Geralf",)


def test_parse_commander_prefers_the_deck_list_spelling():
    known = {"killian, decisive mentor": "Killian, Decisive Mentor"}
    assert parse_commander_field("killian, DECISIVE mentor", known) == ("Killian, Decisive Mentor",)


def test_parse_commander_raises_on_empty_field():
    with pytest.raises(DeckError):
        parse_commander_field("")


def test_parse_commander_raises_rather_than_dropping_an_unresolvable_name():
    with pytest.raises(DeckError, match="not a card in this deck"):
        parse_commander_field("Nonesuch, the Absent", {"sol ring": "Sol Ring"})


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def test_to_dck_is_byte_exact():
    deck = from_text(SILVERQUILL_TEXT, "Silverquill Influence")
    assert to_dck(deck) == (
        "[metadata]\n"
        "Name=Silverquill Influence\n"
        "[Commander]\n"
        "1 Killian, Decisive Mentor\n"
        "[Main]\n"
        "8 Plains\n"
        "8 Swamp\n"
        "1 War Room\n"
    )


def test_to_dck_omits_set_codes():
    deck = from_text("1 Sol Ring (C21) 263", "x")
    assert "|" not in to_dck(deck)


def test_to_dck_is_deterministic_across_input_order():
    first = from_text("1 War Room\n8 Swamp\n8 Plains", "x")
    second = from_text("8 Plains\n1 War Room\n8 Swamp", "x")
    assert to_dck(first) == to_dck(second)


def test_write_dck_round_trips(tmp_path):
    deck = from_text(SILVERQUILL_TEXT, "Silverquill Influence")
    out = write_dck(deck, tmp_path / "nested" / "deck.dck")
    assert out.read_bytes() == to_dck(deck).encode("utf-8")


def test_write_dck_writes_utf8_for_accented_names(tmp_path):
    deck = from_text("Commander\n1 Clavileño, First of the Blessed", "x")
    out = write_dck(deck, tmp_path / "d.dck")
    assert "Clavileño" in out.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def test_validate_accepts_a_legal_deck():
    assert validate(commander_deck()) == []


def test_validate_catches_a_99_card_commander_deck():
    problems = validate(commander_deck(main_size=98))
    assert any("99 cards" in p for p in problems)


def test_validate_catches_a_missing_commander():
    problems = validate(DeckList(name="x", commanders=(), main=((100, "Swamp"),), source="t"))
    assert "no commander" in problems


def test_validate_catches_a_duplicate_nonbasic():
    deck = DeckList(
        name="x",
        commanders=("Killian, Decisive Mentor",),
        main=((2, "Sol Ring"), (97, "Swamp")),
        source="t",
    )
    assert any("copies of a nonbasic" in p for p in validate(deck))


def test_validate_catches_a_nonpositive_quantity():
    deck = DeckList(name="x", commanders=("A",), main=((0, "Sol Ring"),), source="t")
    assert any("not a real number of copies" in p for p in validate(deck))


def test_validate_catches_a_commander_left_in_main():
    deck = DeckList(
        name="x",
        commanders=("Sol Ring",),
        main=((99, "Sol Ring"),),
        source="t",
    )
    assert any("Forge will refuse it" in p for p in validate(deck))


# --------------------------------------------------------------------------- #
# the real collection
# --------------------------------------------------------------------------- #


@needs_db
def test_list_collection_decks_returns_rows(real_collection):
    rows = list_collection_decks()
    assert rows
    assert all(r.slug for r in rows)
    assert {r.owner for r in rows} >= {"matt"}


@needs_db
def test_list_collection_decks_filters_by_owner(real_collection):
    assert {r.owner for r in list_collection_decks(owner="matt")} == {"matt"}


@needs_db
def test_every_collection_deck_loads_and_exports(real_collection):
    """The commander parser has to survive every row that actually exists."""
    for row in list_collection_decks():
        deck = from_collection(row.slug)
        assert deck.commanders, row.slug
        assert deck.size == row.card_count, row.slug
        text = to_dck(deck)
        assert text.startswith("[metadata]\n")
        assert "|" not in text, row.slug


@needs_db
def test_collection_commander_is_not_in_main(real_collection):
    deck = from_collection("silverquill-influence-precon")
    assert deck.commanders == ("Killian, Decisive Mentor",)
    assert all(name != "Killian, Decisive Mentor" for _, name in deck.main)
    # The alternate commander belongs to the 99, Forge only wants one up front.
    assert (1, "Scriv, the Obligator") in deck.main


@needs_db
def test_collection_collapses_the_doubled_face_rows(real_collection):
    """Three decks store the commander as "X // X", which is not a real card."""
    deck = from_collection("feather-storm")
    assert deck.commanders == ("Jetmir, Nexus of Revels",)


@needs_db
def test_collection_uses_front_face_only_for_adventures_and_modal_cards(real_collection):
    names = {name for _, name in from_collection("silverquill-influence-precon").main}
    assert "Defacing Duskmage" in names
    assert "Defacing Duskmage // Vandal's Edit" not in names


@needs_db
def test_collection_keeps_both_halves_of_split_and_room_cards(real_collection):
    assert "Dusk // Dawn" in {name for _, name in from_collection("eternal-might-precon").main}
    assert "Dazzling Theater // Prop Room" in {
        name for _, name in from_collection("feather-storm").main
    }


@pytest.mark.skipif(
    not (HAVE_DB and FORGE_PRECON.exists()),
    reason="needs both the collection database and Forge's bundled precons",
)
def test_export_matches_forges_own_precon_card_for_card(real_collection):
    """The one deck that exists on both sides has to come out identical.

    Forge's file pins a set on every line and names the deck after its file, so
    the comparison is on quantities and card names only.
    """

    def cards(text: str) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        section = ""
        for line in text.splitlines():
            if line.startswith("["):
                section = line
                out.setdefault(section, {})
            elif line.strip() and not (section == "[metadata]" and "=" in line):
                qty, name = line.split(" ", 1)
                out[section][name.split("|")[0]] = int(qty)
        return out

    mine = cards(to_dck(from_collection("silverquill-influence-precon")))
    theirs = cards(FORGE_PRECON.read_text(encoding="utf-8"))
    assert mine["[Commander]"] == theirs["[Commander]"]
    assert mine["[Main]"] == theirs["[Main]"]


@needs_db
def test_collection_keeps_single_cards_that_contain_the_word_and(real_collection):
    assert from_collection("grave-danger-precon").commanders == ("Gisa and Geralf",)
    assert from_collection("jeskai-striker-precon").commanders == ("Shiko and Narset, Unified",)


@needs_db
def test_collection_lookup_by_name_works(real_collection):
    by_slug = from_collection("feather-storm")
    assert from_collection(by_slug.name) == by_slug


@needs_db
def test_collection_unknown_deck_raises(real_collection):
    with pytest.raises(DeckError, match="no deck matching"):
        from_collection("no-such-deck")


@needs_db
def test_collection_decks_are_the_right_size_with_a_commander(real_collection):
    for row in list_collection_decks():
        if row.card_count != 100:
            continue
        problems = validate(from_collection(row.slug))
        assert not [p for p in problems if "Commander wants" in p or p == "no commander"], row.slug


@needs_db
def test_validate_flags_a_duplicate_nonbasic(real_collection):
    """Singleton is the rule, and a repeated nonbasic breaks it.

    Built here rather than read from the collection. An earlier version asserted
    that counter-blitz-precon lists three Blossoming Sands, which it did until
    the deck was edited, and then this test failed for a reason that had nothing
    to do with the validator. A test that breaks when someone fixes their deck
    is testing the wrong thing.
    """
    deck = DeckList(
        name="singleton violation",
        commanders=("Killian, Decisive Mentor",),
        main=((3, "Blossoming Sands"), (60, "Plains"), (36, "Swamp")),
        source="test",
    )
    assert "Blossoming Sands: 3 copies of a nonbasic in a singleton deck" in validate(deck)


def test_validate_allows_duplicates_the_card_itself_permits():
    """Some cards say a deck may run any number, and the validator honours it."""
    deck = DeckList(
        name="hawks",
        commanders=("Jetmir, Nexus of Revels",),
        main=((19, "Tempest Hawk"), (80, "Plains")),
        source="test",
    )
    assert not [p for p in validate(deck) if "Tempest Hawk" in p]


def test_missing_database_raises(tmp_path):
    with pytest.raises(DeckError, match="not found"):
        list_collection_decks(db_dir=tmp_path)


# ---------------------------------------------------- against the fixture

# These run everywhere. The equivalents above assert facts about the
# developer's real decks and skip without them, so the name-shape handling they
# cover was never exercised in CI. The fixture is built to the same shape the
# real importer produces, doubled commander rows included.


def test_fixture_collapses_a_doubled_commander_row() -> None:
    """The importer writes `X // X` for an art-series or reversible printing.

    They are ordinary one-faced cards, so the doubled name matches nothing in
    Forge and the deck will not load.
    """
    assert from_collection("doubled-face").commanders == ("Breya, Etherium Shaper",)


def test_fixture_keeps_a_single_card_whose_name_contains_and() -> None:
    """Splitting on the word invents two commanders that do not exist."""
    assert from_collection("word-and").commanders == ("Gisa and Geralf",)


def test_fixture_keeps_both_halves_of_a_split_card() -> None:
    """Forge spells split cards with both halves and everything else with the
    front face only, so this is the case that must not be trimmed."""
    names = [name for _, name in from_collection("split-halves").main]
    assert "Dusk // Dawn" in names


def test_fixture_uses_the_front_face_of_a_modal_card() -> None:
    names = [name for _, name in from_collection("split-halves").main]
    assert "Agadeem's Awakening" in names
    assert not any(" // " in n for n in names if n.startswith("Agadeem"))


def test_fixture_commander_is_not_repeated_in_main() -> None:
    """Forge rejects a deck listing its commander twice."""
    deck = from_collection("hawk-swarm")
    assert deck.commanders == ("Jetmir, Nexus of Revels",)
    assert not any(name in deck.commanders for _, name in deck.main)


def test_fixture_deck_listing_works_without_a_real_collection() -> None:
    rows = list_collection_decks()
    assert {r.slug for r in rows} >= {"hawk-swarm", "doubled-face", "word-and"}
    assert all(r.owner == "tester" for r in rows)


def test_a_dck_survives_a_round_trip(tmp_path) -> None:
    """Write a deck, read it back, get the same deck.

    Two things broke this and both shipped: from_dck kept the |SETCODE suffix,
    which pins a printing Forge may spell differently and refuse, and the two
    sides sorted differently so the card order drifted.
    """
    deck = DeckList(
        name="round trip",
        commanders=("Jetmir, Nexus of Revels",),
        main=((1, "Sol Ring"), (60, "Plains"), (1, "arcane signet"), (1, "Dusk // Dawn")),
        source="test",
    )
    path = write_dck(deck, tmp_path / "rt.dck")
    back = from_dck(path)

    assert back.commanders == deck.commanders
    # Compared as contents. Order is normalised on both sides, and Forge does
    # not care, but no card and no quantity may change.
    assert dict((n, q) for q, n in back.main) == dict((n, q) for q, n in deck.main)
    # And writing it again is byte-identical, which is what makes two runs of
    # the same deck comparable.
    assert to_dck(back) == to_dck(deck)


def test_from_dck_drops_a_pinned_printing(tmp_path) -> None:
    """Forge's own precons pin a set on every line, and a set its card data
    spells differently is a deck that will not load."""
    p = tmp_path / "pinned.dck"
    p.write_text(
        "[metadata]\nName=Pinned\n"
        "[Commander]\n1 Killian, Decisive Mentor|SOC|1\n"
        "[Main]\n8 Swamp|SOS|3\n1 War Room|SOC|1\n"
    )
    deck = from_dck(p)
    assert deck.commanders == ("Killian, Decisive Mentor",)
    assert (8, "Swamp") in deck.main
    assert not any("|" in name for _, name in deck.main)
