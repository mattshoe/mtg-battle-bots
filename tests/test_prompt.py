"""Tests for the text an agent actually reads, and for parsing what it says back.

The state blocks here use the field names ``StateView.java`` emits, not the
sketch in ARCHITECTURE.md, because the Java side is what the renderer meets at
runtime.

The hidden-information tests are the ones that matter most. A renderer that
leaks an opponent's hand does not fail loudly, it just quietly invalidates every
game played through it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gauntlet.prompt import (
    build_prompt,
    parse_reply,
    render_new_cards,
    render_options,
    render_state,
)
from gauntlet.protocol import VERSION, ProtocolError, Request

CARDS: dict[str, Any] = {
    "cultivate": {"name": "Cultivate", "cost": "{2}{G}", "type": "Sorcery"},
    "forest": {"name": "Forest", "type": "Basic Land - Forest"},
    "llanowar-elves": {"name": "Llanowar Elves", "cost": "{G}", "type": "Creature - Elf Druid"},
    "atraxa": {"name": "Atraxa, Praetors' Voice", "cost": "{G}{W}{U}{B}"},
    "sol-ring": {"name": "Sol Ring", "cost": "{1}"},
    "counterspell": {"name": "Counterspell", "cost": "{U}{U}"},
    # Never on our board or in our graveyard. If this name shows up in a
    # rendering, something is reading a zone it should not.
    "force-of-will": {"name": "Force of Will", "cost": "{3}{U}{U}"},
}

STATE: dict[str, Any] = {
    "turn": 7,
    "phase": "main1",
    "active": "A",
    "you": "A",
    "me": {
        "life": 34,
        "library": 71,
        "hand": ["cultivate", "forest"],
        "battlefield": [
            {"c": "llanowar-elves", "pt": "1/1", "tapped": True, "sick": True},
            {"c": "sol-ring"},
        ],
        "command": ["atraxa"],
        "graveyard": ["forest"],
    },
    "opponents": [
        {
            "name": "B",
            "life": 28,
            "hand_size": 4,
            "library": 68,
            # The bridge does not send this, but if it ever did the renderer
            # must still not print it.
            "hand": ["counterspell", "force-of-will"],
            "battlefield": [{"c": "sol-ring"}, {"c": "llanowar-elves", "pt": "2/2", "dmg": 1}],
            "graveyard": ["counterspell"],
            "command": ["atraxa"],
            "cmd_damage_to_me": {"atraxa": 7},
        }
    ],
    "stack": ["Cultivate", "Counterspell"],
}


def request(**overrides: Any) -> Request:
    raw: dict[str, Any] = {
        "v": VERSION,
        "id": 47,
        "seat": "A",
        "kind": "cast_or_pass",
        "prompt": "Main phase 1. Choose a spell or ability to play.",
        "options": [
            {"i": 0, "label": "Pass priority"},
            {"i": 1, "label": "Cast Cultivate", "cost": "{2}{G}", "card": "cultivate"},
            {"i": 2, "label": "Play Forest", "card": "forest"},
        ],
        "state": STATE,
    }
    raw.update(overrides)
    return Request.parse(json.dumps(raw))


# --------------------------------------------------------------- render_state


def test_render_state_lays_out_a_mid_game_board() -> None:
    assert render_state(STATE, CARDS) == "\n".join(
        [
            "Turn 7, main1, A is the active player.",
            "",
            "YOU (A) - 34 life, 71 cards in library",
            "  Hand (2): Cultivate {2}{G}, Forest",
            "  Battlefield: Llanowar Elves (1/1, tapped, summoning sick), Sol Ring",
            "  Command zone: Atraxa, Praetors' Voice {G}{W}{U}{B}",
            "  Graveyard: Forest",
            "",
            "B - 28 life, 4 cards in hand, 68 in library",
            "  Battlefield: Sol Ring, Llanowar Elves (2/2, 1 damage)",
            "  Graveyard: Counterspell {U}{U}",
            "  Command zone: Atraxa, Praetors' Voice {G}{W}{U}{B}",
            "  Commander damage dealt to you: atraxa 7",
            "",
            "Stack (top last): Cultivate | Counterspell",
        ]
    )


def test_render_state_calls_turn_zero_the_pre_game_window() -> None:
    # Mulligans happen here. "Turn 0, ?, ? is the active player" would invite a
    # seat to reason about a board that does not exist yet.
    pregame = {"turn": 0, "you": "A", "me": {"life": 40, "library": 93, "hand": ["cultivate"]}}

    rendered = render_state(pregame, CARDS)

    assert rendered.startswith("Before the game starts.")
    assert "Turn 0" not in rendered
    assert "  Hand (1): Cultivate {2}{G}" in rendered


def test_render_state_calls_a_missing_phase_the_pre_game_window() -> None:
    # A turn number with no phase is the same window, reached the other way.
    assert render_state({"turn": 1, "you": "A"}, CARDS).startswith("Before the game starts.")


def test_render_state_annotates_a_tapped_summoning_sick_creature() -> None:
    state = {
        "turn": 3,
        "phase": "main1",
        "active": "A",
        "you": "A",
        "me": {"battlefield": [{"c": "llanowar-elves", "pt": "1/1", "tapped": True, "sick": True}]},
    }

    assert "Llanowar Elves (1/1, tapped, summoning sick)" in render_state(state, CARDS)


def test_render_state_omits_annotations_a_permanent_does_not_have() -> None:
    state = {
        "turn": 3,
        "phase": "main1",
        "active": "A",
        "you": "A",
        "me": {"battlefield": [{"c": "sol-ring"}]},
    }
    rendered = render_state(state, CARDS)

    assert "  Battlefield: Sol Ring\n" in rendered + "\n"
    assert "tapped" not in rendered
    assert "summoning sick" not in rendered


def test_render_state_keeps_an_opponents_hand_a_count() -> None:
    # The whole harness is worthless if a seat can read an opponent's hand, so
    # this asserts on the card names rather than on the phrasing.
    rendered = render_state(STATE, CARDS)

    assert "4 cards in hand" in rendered
    assert "Force of Will" not in rendered
    assert "force-of-will" not in rendered
    # Counterspell is in the opponent's graveyard, which is public, and in the
    # hidden hand. One copy of the name, from the graveyard line.
    assert rendered.count("Counterspell") == 2
    assert "  Graveyard: Counterspell {U}{U}" in rendered
    assert "Stack (top last): Cultivate | Counterspell" in rendered


def test_render_state_shows_the_slug_for_a_card_it_has_no_text_for() -> None:
    # Better than a blank. It happens when a card enters a visible zone in the
    # same message the seat is asked about.
    state = {"turn": 2, "phase": "main1", "active": "A", "you": "A", "me": {"hand": ["mystery"]}}

    assert "  Hand (1): mystery" in render_state(state, CARDS)


def test_render_state_says_empty_and_dash_for_bare_zones() -> None:
    state = {"turn": 2, "phase": "main1", "active": "A", "you": "A", "me": {}}
    rendered = render_state(state, CARDS)

    assert "  Hand (0): -" in rendered
    assert "  Battlefield: empty" in rendered
    # An empty graveyard or command zone gets no line at all, not a blank one.
    assert "Graveyard" not in rendered
    assert "Command zone" not in rendered


# ----------------------------------------------------- the rest of the prompt


def test_render_new_cards_spells_out_oracle_text() -> None:
    fresh = {
        "cultivate": {
            "name": "Cultivate",
            "cost": "{2}{G}",
            "type": "Sorcery",
            "text": "Search your library for up to two basic land cards.\n\nThen shuffle.",
        }
    }

    assert render_new_cards(fresh) == "\n".join(
        [
            "Cards you have not seen yet this game:",
            "  Cultivate {2}{G} - Sorcery",
            "      Search your library for up to two basic land cards.",
            "      Then shuffle.",
        ]
    )


def test_render_new_cards_is_empty_when_nothing_is_new() -> None:
    assert render_new_cards({}) == ""


def test_render_options_numbers_every_choice() -> None:
    assert render_options(request()) == "\n".join(
        [
            "Your options:",
            "  [0] Pass priority",
            "  [1] Cast Cultivate  cost {2}{G}",
            "  [2] Play Forest",
        ]
    )


def test_render_options_surfaces_what_forge_would_have_done() -> None:
    # "proposed" arrives through extra, so this is the forward-compat path
    # working end to end rather than a field the dataclass models.
    assert "Forge proposes: 1, 2" in render_options(request(proposed=[1, 2]))


def test_build_prompt_puts_the_deck_note_first_and_the_options_last() -> None:
    prompt = build_prompt(request(), CARDS, deck_note="Atraxa superfriends, grind to a wide board")

    assert prompt.startswith("Your deck: Atraxa superfriends, grind to a wide board")
    assert prompt.rstrip().endswith("[2] Play Forest")
    assert "Main phase 1. Choose a spell or ability to play." in prompt
    assert "Force of Will" not in prompt


# ---------------------------------------------------------------- parse_reply


def test_parse_reply_reads_a_well_formed_answer() -> None:
    text = "CHOICE: 1\nWHY: Ramp now and hold nothing up. Curve is the constraint."

    assert parse_reply(text, request()) == (
        1,
        "Ramp now and hold nothing up. Curve is the constraint.",
    )


def test_parse_reply_accepts_a_bare_number() -> None:
    # Common enough from a model that wandered off format, and the alternative
    # is Forge quietly taking the turn.
    assert parse_reply("2", request()) == (2, "")


def test_parse_reply_accepts_a_bracketed_number_on_its_own_line() -> None:
    assert parse_reply("Thinking about it.\n[0]\n", request()) == (0, "")


def test_parse_reply_ignores_case_and_surrounding_chatter() -> None:
    text = "Sure, here goes.\n\n  choice : 1\n  why : keep the elf back\n\nHope that helps."

    choice, why = parse_reply(text, request())

    assert choice == 1
    assert why.startswith("keep the elf back")


def test_parse_reply_collapses_an_essay_into_one_line() -> None:
    text = "CHOICE: 0\nWHY: " + "\n".join(["reason " + str(n) for n in range(200)])

    _, why = parse_reply(text, request())

    assert "\n" not in why
    assert len(why) <= 500


def test_parse_reply_rejects_a_choice_that_is_not_on_offer() -> None:
    with pytest.raises(ProtocolError, match=r"chose 9, options are \[0, 1, 2\]"):
        parse_reply("CHOICE: 9\nWHY: I would rather do something else", request())


def test_parse_reply_rejects_a_reply_with_no_number_in_it() -> None:
    with pytest.raises(ProtocolError, match="no CHOICE in reply"):
        parse_reply("I think we should probably just pass here.", request())


# --------------------------------- the two fixes with prose but no test

# Both of these are documented incidents in CLAUDE.md and both could be deleted
# with the suite green. No test had ever passed a `since` or a `combat` key.


def test_combat_is_rendered_so_a_seat_can_see_what_is_attacking() -> None:
    """A seat asked to block used to be shown a board where the only clue was
    which creatures happened to be tapped, and vigilance removed even that."""
    state = {
        "turn": 8,
        "phase": "combat_declare_blockers",
        "active": "B",
        "you": "A",
        "me": {"life": 34, "library": 70},
        "combat": [
            {"c": "scourge-of-fleets", "pt": "6/6", "attacking": "A"},
            {
                "c": "anowon-the-ruin-thief",
                "pt": "2/4",
                "attacking": "A",
                "blocked_by": ["tempest-hawk"],
            },
        ],
    }
    cards = {
        "scourge-of-fleets": {"name": "Scourge of Fleets"},
        "anowon-the-ruin-thief": {"name": "Anowon, the Ruin Thief"},
        "tempest-hawk": {"name": "Tempest Hawk"},
    }
    out = render_state(state, cards)

    assert "In combat:" in out
    assert "Scourge of Fleets 6/6 attacking A" in out
    assert "Anowon, the Ruin Thief 2/4 attacking A, blocked by Tempest Hawk" in out


def test_no_combat_block_when_nothing_is_attacking() -> None:
    out = render_state({"turn": 3, "phase": "main1", "active": "A", "me": {}}, {})
    assert "In combat:" not in out


def test_what_happened_since_the_last_decision_is_shown() -> None:
    """Forge chooses targets, so a spell a seat picked can fizzle or hit
    something else, and the board alone cannot say which. Without this feed a
    removal spell left hand and mana and killed nothing, silently."""
    from gauntlet.prompt import render_since

    events = [
        "A casts Price of Fame targeting Jetmir, Nexus of Revels",
        "Price of Fame fizzles, no legal target",
    ]
    out = render_since(events)
    assert "Since your last decision:" in out
    assert "fizzles" in out
    assert render_since([]) == ""


def test_the_since_feed_reaches_the_built_prompt() -> None:
    """render_since existing is not the same as build_prompt using it."""
    request = Request.parse(
        json.dumps(
            {
                "v": VERSION,
                "id": 1,
                "seat": "A",
                "kind": "cast_or_pass",
                "prompt": "priority",
                "options": [{"i": 0, "label": "Pass priority"}],
                "state": {"turn": 5, "phase": "main1", "active": "A", "me": {}},
                "since": ["B's Combat Damage Step", "A loses 6 life"],
            }
        )
    )
    built = build_prompt(request, {})
    assert "Since your last decision:" in built
    assert "A loses 6 life" in built
