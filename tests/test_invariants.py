"""The properties the whole harness rests on.

CONTRIBUTING lists seven invariants. Most were enforced by nothing, which meant
breaking one produced a green suite and a plausible-looking result. These tests
are deliberately blunt, because the cost of each violation is that every number
this project produces stops meaning anything.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
from pathlib import Path

import pytest

from gauntlet import paths
from gauntlet.protocol import VERSION, Request
from gauntlet.server import MatchServer
from gauntlet.transcript import Transcript

REPO = Path(__file__).resolve().parent.parent
JAVA = REPO / "java" / "src" / "main" / "java" / "forge" / "gauntlet"


# ------------------------------------------- 4. hidden information stays hidden


#: What a seat is allowed to know about an opponent. Anything else is either a
#: leak or a field nobody has thought about, and both should fail loudly.
ALLOWED_OPPONENT_KEYS = frozenset(
    {
        "name",
        "life",
        "hand_size",
        "library",
        "battlefield",
        "graveyard",
        "command",
        "exile",
        "cmd_damage_to_me",
    }
)

#: Fields that must be a count and never a list. `library` as a list is the
#: whole deck order, `hand_size` becoming `hand` is the opponent's hand.
MUST_BE_COUNTS = ("hand_size", "library")


def assert_no_leak(state: dict) -> None:
    """Fail on anything in a state block a seat must not be able to see."""
    for opponent in state.get("opponents") or []:
        unexpected = set(opponent) - ALLOWED_OPPONENT_KEYS
        assert not unexpected, f"unknown opponent field(s), possible leak: {unexpected}"

        assert "hand" not in opponent, "an opponent's hand contents reached a seat"
        for field in MUST_BE_COUNTS:
            value = opponent.get(field)
            assert not isinstance(value, list), f"opponent {field} is a list, not a count"

    me = state.get("me") or {}
    # Your own library is hidden from you too, or a seat could play around its
    # own next draw.
    assert not isinstance(me.get("library"), list), "your own library order reached you"


def test_the_leak_detector_actually_detects_a_leak() -> None:
    """A guard nobody has seen fail is a guard nobody should trust."""
    with pytest.raises(AssertionError, match="hand"):
        assert_no_leak({"opponents": [{"name": "B", "hand": ["sol-ring"]}]})
    with pytest.raises(AssertionError, match="library"):
        assert_no_leak({"opponents": [{"name": "B", "library": ["forest", "island"]}]})
    with pytest.raises(AssertionError, match="own library"):
        assert_no_leak({"me": {"library": ["forest"]}})
    with pytest.raises(AssertionError, match="possible leak"):
        assert_no_leak({"opponents": [{"name": "B", "decklist": ["x"]}]})


def test_a_clean_state_passes_the_leak_detector() -> None:
    assert_no_leak(
        {
            "me": {"life": 40, "hand": ["sol-ring"], "library": 92},
            "opponents": [{"name": "B", "life": 38, "hand_size": 5, "library": 90}],
        }
    )


def test_the_daemon_forwards_whatever_state_view_sent(tmp_path) -> None:
    """Named for what it actually asserts.

    It used to be called "carries no hidden information" while its body proved
    with pytest.raises that a leak reaches the agent untouched. That is a real
    and useful characterisation, the daemon is a pass-through by design and the
    defence lives in StateView, but the old name claimed a guarantee nobody
    had.

    `server._control_op` returns `request.state` verbatim to an interactive
    agent. The only previous test was on the rendered prose, which ignores
    fields it does not know about, so a leak in StateView would have reached an
    agent while the suite stayed green.
    """
    from gauntlet.seats import InteractiveSeat

    leaky = {
        "turn": 3,
        "phase": "main1",
        "active": "A",
        "you": "A",
        "me": {"life": 40, "hand": ["sol-ring"], "library": 92},
        "opponents": [
            {
                "name": "B",
                "life": 38,
                "hand_size": 4,
                "library": 90,
                # The leak, as it would arrive from a broken StateView.
                "hand": ["counterspell", "swords-to-plowshares"],
            }
        ],
    }
    wire = json.dumps(
        {
            "v": VERSION,
            "id": 1,
            "seat": "A",
            "kind": "cast_or_pass",
            "prompt": "priority",
            "options": [{"i": 0, "label": "Pass priority"}],
            "state": leaky,
        }
    )

    server = MatchServer(
        match_id="leak",
        seats={"A": InteractiveSeat()},
        transcript=Transcript(tmp_path / "t.db"),
        decision_timeout=1.0,
    )
    endpoint, _ = server.bind()
    server.start()
    host, port = endpoint.split(":")
    try:
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            sock.sendall(wire.encode() + b"\n")
            from gauntlet.server import call_match

            reply = call_match("leak", {"op": "act", "seat": "A", "timeout": 3})

        handed = reply["request"]["state"]
        # A pass-through, verbatim. Which is why the Java-side assertions below
        # are the thing actually defending this invariant.
        assert handed == leaky
        with pytest.raises(AssertionError):
            assert_no_leak(handed)
    finally:
        server.shutdown()


def test_state_view_sends_an_opponent_nothing_outside_the_allowed_keys() -> None:
    """Every key StateView puts on an opponent, checked against the whitelist.

    The previous version grepped for two specific spellings, so adding
    `op.add("hand_cards", refs(p.getCardsIn(ZoneType.Hand)))` shipped the
    opponent's entire hand with the suite green. Reading the keys out and
    comparing the set catches a leak under any name.
    """
    source = (JAVA / "StateView.java").read_text()
    start = source.index("JsonArray opps")
    block = source[start : source.index('o.add("opponents"')]

    emitted = set(re.findall(r'op\.(?:add|addProperty)\(\s*"([A-Za-z_]+)"', block))
    assert emitted, "no opponent fields found, the block moved and this check is blind"

    unexpected = emitted - ALLOWED_OPPONENT_KEYS
    assert not unexpected, (
        f"StateView sends opponent field(s) not on the whitelist: {sorted(unexpected)}. "
        "Add them to ALLOWED_OPPONENT_KEYS only if a player may really see them."
    )


def test_no_opponent_zone_a_player_cannot_see_is_read_at_all() -> None:
    """Belt and braces, on the zone rather than the field name.

    A leak needs both a hidden zone and a key to put it under. This catches the
    first half however the second is spelled.
    """
    source = (JAVA / "StateView.java").read_text()
    start = source.index("JsonArray opps")
    block = source[start : source.index('o.add("opponents"')]

    for hidden in ("ZoneType.Hand", "ZoneType.Library"):
        # A size() call is a count, which is public. Anything else reads cards.
        for match in re.finditer(re.escape(hidden), block):
            tail = block[match.end() : match.end() + 40]
            assert ".size()" in tail, (
                f"{hidden} is read in the opponent block without being reduced "
                f"to a count: ...{tail.strip()[:40]}"
            )


def test_your_own_library_order_is_never_sent() -> None:
    """Hidden from you as well, or a seat plays around its own next draw."""
    source = (JAVA / "StateView.java").read_text()
    start = source.index("JsonObject you = new JsonObject()")
    me_block = source[start : source.index('o.add("me"')]

    assert 'you.addProperty("library"' in me_block
    assert 'you.add("library"' not in me_block


# ----------------------------------- 5. the protocol is versioned on both sides


def test_the_two_sides_agree_on_the_protocol_version() -> None:
    """Nothing checked this, and drift is silent until a message is rejected
    mid-run, hours in."""
    java = (JAVA / "Bridge.java").read_text()
    match = re.search(r"PROTOCOL_VERSION\s*=\s*(\d+)", java)
    assert match, "Bridge.java no longer declares PROTOCOL_VERSION"
    assert int(match.group(1)) == VERSION, (
        f"Bridge.java speaks v{match.group(1)}, protocol.py speaks v{VERSION}"
    )


def test_a_message_from_a_future_protocol_is_refused() -> None:
    from gauntlet.protocol import ProtocolError

    with pytest.raises(ProtocolError, match="version"):
        Request.parse(json.dumps({"v": VERSION + 1, "id": 1, "kind": "cast_or_pass"}))


# --------------------------- 2. a fallback is never disguised as a play


def test_a_fallback_never_carries_reasoning(tmp_path) -> None:
    """The literal statement of the invariant, as a database assertion.

    A row with fallback=1 and a non-empty why would be Forge's play wearing an
    agent's words, which is the one thing this harness must never produce.
    """
    import sqlite3

    from gauntlet.protocol import Response

    t = Transcript(tmp_path / "t.db")
    t.start_match(
        match_id="m",
        format="Commander",
        seed=1,
        seats=[{"seat": "A", "controller": "sdk"}],
    )
    request = Request.parse(
        json.dumps(
            {
                "v": VERSION,
                "id": 1,
                "seat": "A",
                "kind": "cast_or_pass",
                "prompt": "p",
                "options": [{"i": 0, "label": "Pass priority"}],
                "state": {},
            }
        )
    )
    t.record_decision(
        match_id="m",
        seat="A",
        request=request,
        response=None,
        latency_ms=1,
        fallback=True,
        fallback_reason="timed out",
    )
    t.record_decision(
        match_id="m",
        seat="A",
        request=request,
        response=Response(id=1, choice=0, why="a real reason"),
        latency_ms=1,
    )
    t.close()

    conn = sqlite3.connect(tmp_path / "t.db")
    bad = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE fallback = 1 AND why != ''"
    ).fetchone()[0]
    conn.close()
    assert bad == 0, "a fallback was recorded carrying an agent's reasoning"


# --------------------------------- 1, 6, 7. rules, pinning, and the licence split


def test_no_magic_rules_live_in_the_python() -> None:
    """Magic's rules are Forge's.

    A harness that starts special-casing cards stops being a harness. Card names
    appear legitimately in exactly one place, the basic land list, which is a
    deckbuilding rule rather than a game rule.
    """
    offenders = []
    for path in (REPO / "src" / "gauntlet").glob("*.py"):
        if path.name == "decks.py":
            continue  # BASIC_LANDS, see the docstring above
        text = path.read_text()
        for needle in ("Lightning Bolt", "Sol Ring", "counterspell", "first strike", "trample"):
            if needle.lower() in text.lower():
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, f"Magic rules or card names leaked into the harness: {offenders}"


def test_forge_is_not_vendored_into_git() -> None:
    assert "vendor/" in (REPO / ".gitignore").read_text()
    tracked = subprocess.run(
        ["git", "ls-files", "vendor/"], cwd=REPO, capture_output=True, text=True, check=False
    )
    # A failed git call used to pass this vacuously, since its stdout is empty
    # too. Prove git worked before believing what it said.
    if tracked.returncode != 0:
        pytest.skip(f"git is unavailable here: {tracked.stderr.strip()[:80]}")
    proof = subprocess.run(
        ["git", "ls-files", "src/"], cwd=REPO, capture_output=True, text=True, check=False
    )
    assert proof.stdout.strip(), "git listed no tracked files at all, so this proves nothing"
    assert not tracked.stdout.strip(), "Forge has been committed to the repository"


def test_the_licence_boundary_holds() -> None:
    """GPLv3 bridge, MIT python, talking over a socket.

    Collapsing that boundary would make the python GPL too, which is a licensing
    change nobody would notice in a diff.
    """
    for path in (REPO / "src" / "gauntlet").glob("*.py"):
        text = path.read_text()
        assert "GNU General Public" not in text, f"{path.name} carries a GPL header"

    for path in JAVA.glob("*.java"):
        header = path.read_text()[:400]
        assert "GPLv3" in header or "GPL-3" in header or "General Public" in header, (
            f"{path.name} does not carry its licence in the header"
        )


def test_the_pinned_forge_version_matches_what_the_fetch_script_installs() -> None:
    script = (REPO / "scripts" / "fetch-forge.sh").read_text()
    pinned = re.search(r'FORGE_VERSION="\$\{FORGE_VERSION:-([\d.]+)\}"', script)
    assert pinned, "fetch-forge.sh no longer pins a version"

    try:
        installed = paths.forge_version()
    except FileNotFoundError:
        pytest.skip("no Forge installed here, nothing to compare against")
    assert installed == pinned.group(1), (
        f"installed Forge is {installed}, the script pins {pinned.group(1)}"
    )


# ------------------------------- the four-step checklist, enforced mechanically


def test_the_decision_kinds_agree_across_the_bridge() -> None:
    """CLAUDE.md's checklist for adding a kind has four steps and only the
    protocol version was ever checked.

    Rename a kind in Java and Python keeps sending a name the bridge ignores,
    so the seat is bridged, routed nothing, and plays no decisions.
    """
    from gauntlet.protocol import KINDS

    controller = (JAVA / "PlayerControllerGauntlet.java").read_text()
    java_kinds = set(re.findall(r'routes\("([a-z_]+)"\)', controller))

    assert java_kinds, "no routed kinds found in PlayerControllerGauntlet.java"
    assert java_kinds == set(KINDS), (
        f"java routes {sorted(java_kinds)}, protocol.KINDS is {sorted(KINDS)}"
    )


def test_the_default_routed_set_agrees_across_the_bridge() -> None:
    from gauntlet.match import DEFAULT_ROUTED

    main = (JAVA / "GauntletMain.java").read_text()
    block = main[main.index("DEFAULT_ROUTED") : main.index("public static void main")]
    java_default = set(re.findall(r'"([a-z_]+)"', block))

    assert java_default == set(DEFAULT_ROUTED), (
        f"java defaults to {sorted(java_default)}, python to {sorted(DEFAULT_ROUTED)}"
    )


def test_state_view_keeps_your_own_library_a_count_too() -> None:
    """The existing check slices only the opponent block, so the `me` block
    could start sending your library order and nothing would fail.

    Your own library is hidden from you as well, or a seat plays around its
    own next draw.
    """
    source = (JAVA / "StateView.java").read_text()
    start = source.index("JsonObject you = new JsonObject()")
    me_block = source[start : source.index('o.add("me"')]

    assert 'you.addProperty("library"' in me_block
    assert 'you.add("library"' not in me_block, "StateView sends your own library order"
