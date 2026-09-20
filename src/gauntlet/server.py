"""The match daemon.

One object owns a running match. It listens on two sockets:

* a TCP socket on loopback that the Forge bridge connects to, one connection per
  bridged player, carrying decision requests
* a unix socket that ``gauntlet act`` connects to, carrying an agent's answers
  and collecting the next question

Between them sit the seats. A request arrives on a bridge connection, is routed
to the seat named in it, and the thread serving that connection blocks until the
seat answers or gives up. Forge is single-threaded per game, so blocking there
costs nothing that was not already stopped.

Everything that happens is written to the transcript as it happens, not at the
end. A match that crashes must leave behind everything up to the crash.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .protocol import NOTIFICATIONS, ProtocolError, Request, Response
from .seats import InteractiveSeat, Seat, SeatExhausted, SeatTimeout
from .transcript import Transcript


@dataclass(slots=True)
class MatchResult:
    """What a finished match tells the caller."""

    match_id: str
    games: list[dict[str, Any]] = field(default_factory=list)
    crashed: bool = False
    error: str = ""
    #: Seats that ran out of capacity. Non-empty invalidates the result.
    exhausted: dict[str, str] = field(default_factory=dict)

    def wins_by_seat(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for g in self.games:
            winner = g.get("winner")
            if winner:
                out[winner] = out.get(winner, 0) + 1
        return out


def _defer(request: Request) -> bytes:
    """The answer that means "you decide, Forge".

    A null choice rather than a missing field, so a reader of the raw stream can
    tell a deliberate hand-back from a truncated message.
    """
    return json.dumps({"v": 1, "id": request.id, "choice": None}).encode() + b"\n"


class MatchServer:
    """Serves one match until Forge exits."""

    def __init__(
        self,
        *,
        match_id: str,
        seats: dict[str, Seat],
        transcript: Transcript,
        decision_timeout: float = 300.0,
    ) -> None:
        self.match_id = match_id
        self.seats = seats
        self.transcript = transcript
        self.decision_timeout = decision_timeout

        self.result = MatchResult(match_id=match_id)
        self.finished = threading.Event()
        #: Seats that ran out of capacity, by seat name. Non-empty means the
        #: run's numbers are not what they claim to be.
        self.exhausted: dict[str, str] = {}

        self._tcp: socket.socket | None = None
        self._ctl: socket.socket | None = None
        self._ctl_path: Path | None = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Card text already sent to each seat, so a stateless `act` call can be
        # handed the names for whatever is on the board right now.
        self._cards: dict[str, dict[str, Any]] = {}
        # Slugs whose rules text has already been rendered into an act reply.
        self._shown: dict[str, set[str]] = {}
        # Last (question id, rules text) handed to each seat, so re-asking
        # the same question returns the same answer.
        self._last_render: dict[str, tuple[int, dict[str, Any]]] = {}

    # ------------------------------------------------------------- lifecycle

    def bind(self) -> tuple[str, Path]:
        """Open both sockets and return where they are.

        Binding to port 0 and reading back the assignment avoids the race of
        picking a free port and hoping it is still free by the time Forge
        starts. Several matches run at once on a busy machine.
        """
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(8)
        tcp.settimeout(1.0)
        self._tcp = tcp
        host, port = tcp.getsockname()

        ctl_path = paths.match_socket(self.match_id)
        ctl_path.unlink(missing_ok=True)
        ctl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ctl.bind(str(ctl_path))
        ctl.listen(8)
        ctl.settimeout(1.0)
        self._ctl = ctl
        self._ctl_path = ctl_path

        return f"{host}:{port}", ctl_path

    def start(self) -> None:
        for target, name in ((self._accept_bridges, "bridges"), (self._accept_control, "control")):
            t = threading.Thread(target=target, name=f"gauntlet-{name}", daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self) -> None:
        self._stop.set()
        self.finished.set()
        for seat in self.seats.values():
            seat.close()
        for sock in (self._tcp, self._ctl):
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
        if self._ctl_path is not None:
            self._ctl_path.unlink(missing_ok=True)

    # -------------------------------------------------------- bridge side

    def _accept_bridges(self) -> None:
        assert self._tcp is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._tcp.accept()
            except (TimeoutError, OSError):
                continue
            t = threading.Thread(target=self._serve_bridge, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _serve_bridge(self, conn: socket.socket) -> None:
        """Answer one bridged player's questions until it hangs up."""
        conn.settimeout(None)
        with conn, conn.makefile("rwb") as stream:
            while not self._stop.is_set():
                line = stream.readline()
                if not line:
                    return
                try:
                    request = Request.parse(line)
                except ProtocolError as exc:
                    # A message we cannot read means the two sides disagree
                    # about the protocol. Carrying on would silently corrupt the
                    # transcript, so record it and drop the connection.
                    self.transcript.record_event(
                        match_id=self.match_id,
                        kind="protocol_error",
                        payload={"error": str(exc), "line": line[:500].decode("utf-8", "replace")},
                    )
                    return

                if request.kind in NOTIFICATIONS or not request.wants_answer:
                    self._handle_notification(request)
                    continue

                reply = self._decide(request)
                try:
                    stream.write(reply)
                    stream.flush()
                except OSError:
                    return

    def _decide(self, request: Request) -> bytes:
        """Route one request to its seat and encode the answer.

        A seat that times out, errors, or is not configured produces an empty
        answer, which the bridge reads as "you decide". The transcript records
        it as a fallback with the reason, so a run where the agent was asleep
        cannot be mistaken for one where it played badly.
        """
        seat = self.seats.get(request.seat)
        started = time.monotonic()
        self._remember_cards(request)

        if seat is None:
            self._record(request, None, started, f"no seat configured for {request.seat!r}")
            return _defer(request)

        try:
            response = seat.decide(request, self.decision_timeout)
            response.validate_against(request)
        except SeatExhausted as exc:
            # Every remaining decision for this seat will fail the same way.
            # Falling back silently would finish the run and report Forge's play
            # as the agent's, which is the one thing this harness must never do.
            self.exhausted[request.seat] = str(exc)
            self._record(request, None, started, f"seat exhausted: {exc}")
            self.transcript.record_event(
                match_id=self.match_id,
                kind="seat_exhausted",
                payload={"seat": request.seat, "reason": str(exc)},
            )
            self.finished.set()
            return _defer(request)
        except (SeatTimeout, ProtocolError) as exc:
            self._record(request, None, started, str(exc))
            return _defer(request)
        except Exception as exc:
            # A seat blowing up is a bug in the seat, not a reason to end the
            # game. Log loudly, let Forge play the turn.
            self._record(request, None, started, f"seat raised {type(exc).__name__}: {exc}")
            return _defer(request)

        self._record(request, response, started, "")
        return response.encode()

    def _remember_cards(self, request: Request) -> None:
        """Keep every card this seat has been told about.

        The bridge sends a card's text once and never again, which keeps the
        round trip small. That works for a seat that holds state across
        decisions, and `gauntlet act` does not - it is a fresh process each
        call. So the daemon remembers on its behalf and hands back whatever the
        current board refers to.
        """
        if not request.new_cards:
            return
        with self._lock:
            self._cards.setdefault(request.seat, {}).update(request.new_cards)

    def _cards_in_view(self, request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
        """Split what a seat needs to see into names and full text.

        Two dictionaries, because they answer different questions. The first
        names everything currently in view, and an agent needs all of it every
        call to read the board at all. The second carries rules text, and an
        agent only needs that the first time it meets a card.

        Tracking what has already been spelled out is the daemon's job rather
        than the bridge's here, because the bridge counts what it sent to the
        socket and the socket is not what the agent read.
        """
        with self._lock:
            known = dict(self._cards.get(request.seat, {}))

        wanted: set[str] = {o.card for o in request.options if o.card}

        def walk(node: Any) -> None:
            if isinstance(node, str):
                if node in known:
                    wanted.add(node)
            elif isinstance(node, dict):
                for key, value in node.items():
                    if key == "c" and isinstance(value, str):
                        wanted.add(value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(request.state)
        in_view = {slug: known[slug] for slug in sorted(wanted) if slug in known}

        with self._lock:
            # Asking for the same question twice must give the same answer
            # twice, rules text included. `take` is idempotent by design, so an
            # agent that lost its place calls again, and marking cards as shown
            # on the first call would leave the second one holding a board of
            # cards it was never told the text of.
            last_id, last_fresh = self._last_render.get(request.seat, (None, {}))
            if last_id == request.id:
                fresh = last_fresh
            else:
                shown = self._shown.setdefault(request.seat, set())
                fresh = {slug: detail for slug, detail in in_view.items() if slug not in shown}
                shown.update(fresh)
                self._last_render[request.seat] = (request.id, fresh)

        # Names and costs are cheap and needed every time. Rules text is the
        # expensive half, so it goes out once.
        names = {
            slug: {k: v for k, v in detail.items() if k != "text"}
            for slug, detail in in_view.items()
        }
        return names, fresh

    def _record(
        self,
        request: Request,
        response: Response | None,
        started: float,
        fallback_reason: str,
    ) -> None:
        self.transcript.record_decision(
            match_id=self.match_id,
            seat=request.seat,
            request=request,
            response=response,
            latency_ms=int((time.monotonic() - started) * 1000),
            fallback=response is None,
            fallback_reason=fallback_reason,
        )

    def record_game_result(self, payload: dict[str, Any]) -> None:
        """Record the end of one game, from whichever channel reported it.

        Two channels report it and both are needed. Forge prints it on stdout,
        which is the only channel a match with no bridged seat has at all. It
        also pushes it over each bridge, which is how an interactive agent
        learns the game ended rather than waiting out its poll. Whichever
        arrives first wins and the rest are ignored.
        """
        # One sentinel on both sides. Reading the stored payloads with a
        # different default from the incoming one meant a result with no game
        # number never matched itself, and got counted twice.
        game_no = int(payload.get("game", 0))
        with self._lock:
            if any(int(g.get("game", 0)) == game_no for g in self.result.games):
                return
            self.result.games.append(payload)

        self.transcript.record_game(
            match_id=self.match_id,
            game_no=game_no,
            winner_seat=payload.get("winner"),
            draw=bool(payload.get("draw", False)),
            turns=int(payload.get("turns", 0)),
            ms=int(payload.get("ms", 0)),
        )

    def _handle_notification(self, request: Request) -> None:
        payload = dict(request.extra)
        if request.kind == "game_result":
            self.record_game_result(payload)
        else:
            self.transcript.record_event(
                match_id=self.match_id, kind=request.kind, payload=payload
            )

    # ------------------------------------------------------- control side

    def _accept_control(self) -> None:
        assert self._ctl is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._ctl.accept()
            except (TimeoutError, OSError):
                continue
            t = threading.Thread(target=self._serve_control, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _serve_control(self, conn: socket.socket) -> None:
        """One request, one reply. An agent's call is not a session."""
        conn.settimeout(None)
        with conn, conn.makefile("rwb") as stream:
            line = stream.readline()
            if not line:
                return
            try:
                msg = json.loads(line)
            except ValueError as exc:
                self._reply(stream, {"status": "error", "error": f"not JSON: {exc}"})
                return
            try:
                self._reply(stream, self._control_op(msg))
            except Exception as exc:
                self._reply(stream, {"status": "error", "error": f"{type(exc).__name__}: {exc}"})

    @staticmethod
    def _reply(stream, payload: dict[str, Any]) -> None:
        stream.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
        stream.flush()

    def _control_op(self, msg: dict[str, Any]) -> dict[str, Any]:
        op = msg.get("op")
        if op == "status":
            return self._status()
        if op == "stop":
            self.shutdown()
            return {"status": "stopped"}
        if op != "act":
            return {"status": "error", "error": f"unknown op {op!r}"}

        seat_name = msg.get("seat")
        seat = self.seats.get(seat_name)
        if not isinstance(seat, InteractiveSeat):
            return {
                "status": "error",
                "error": f"seat {seat_name!r} is not interactive, nothing to act on",
            }

        # Submitting is separate from collecting on purpose. An agent that
        # crashed mid-turn restarts by calling with no choice, and picks up the
        # question still on the table rather than having to guess where it was.
        if msg.get("choice") is not None:
            # Resolving the target id here rather than in the caller is what
            # makes it safe. An agent that omits the id means "the question you
            # just showed me", and only the daemon knows which that was. A
            # caller that probed for the open id would sometimes find a
            # different question, its own answer having arrived too late, and
            # would then answer that one with the wrong choice.
            target = msg.get("id")
            if target is None:
                target = seat.open_id()
                if target is None:
                    return {
                        "status": "stale",
                        "error": (
                            "the question you were shown is no longer open, most likely your "
                            "answer arrived after the engine gave up on it. Call again with no "
                            "--choice to see where the game actually is."
                        ),
                    }
            try:
                seat.answer(
                    Response(
                        id=int(target),
                        choice=int(msg["choice"]),
                        why=str(msg.get("why", "")),
                    )
                )
            except (ProtocolError, KeyError, ValueError) as exc:
                return {"status": "error", "error": str(exc)}

        timeout = float(msg.get("timeout", 120.0))
        request = seat.take(timeout)
        if request is None:
            if self.finished.is_set():
                return {"status": "game_over", **self._status()}
            return {"status": "waiting"}

        names, fresh = self._cards_in_view(request)
        return {
            "status": "decide",
            "request": {
                "id": request.id,
                "seat": request.seat,
                "kind": request.kind,
                "prompt": request.prompt,
                "options": [
                    {"i": o.index, "label": o.label, "card": o.card, "cost": o.cost}
                    for o in request.options
                ],
                "state": request.state,
                "cards": names,
                "new_cards": fresh,
                **request.extra,
            },
        }

    def _status(self) -> dict[str, Any]:
        with self._lock:
            games = list(self.result.games)
        return {
            "match": self.match_id,
            "finished": self.finished.is_set(),
            "games": games,
            "wins": self.result.wins_by_seat(),
            "seats": {name: seat.controller for name, seat in self.seats.items()},
        }


def call_match(match_id: str, payload: dict[str, Any], *, timeout: float = 600.0) -> dict[str, Any]:
    """Send one control message to a running match and read the reply.

    Used by the CLI. Kept here rather than in the CLI so the control protocol
    has exactly one definition.
    """
    path = paths.match_socket(match_id)
    if not path.exists():
        raise FileNotFoundError(f"no running match {match_id!r} (looked for {path})")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
        with sock.makefile("rb") as stream:
            line = stream.readline()
    if not line:
        raise ConnectionError(f"match {match_id!r} closed the connection without replying")
    return json.loads(line)
