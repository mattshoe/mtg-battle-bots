# Feather Storm vs Kayla's decks — VOID, do not use these numbers

160 games, 8 opponents, 20 each, intended as the first agent-versus-agent run.
The raw sweep output is in `data/`. The win rates in it are not a measurement of
anything and no report was generated from them.

## What happened

Both seats were configured as `sdk`, so both were meant to be played by Claude.
About forty minutes in, the Claude Code session hit its usage limit. The seat
asked for a decision and got back:

```
You've hit your session limit · resets 3pm (America/New_York)
```

The seat treated that the way it treated any unparseable reply: one failed
decision, fall back to Forge's AI, carry on. It then did that 13,598 more times
over the next two hours and reported a clean 95-65 result with zero errors.

Of 22,445 decisions, **17,851 (79.5%) were made by Forge's AI**, not by an
agent. The headline number would have been a Forge-versus-Forge result wearing
an agent's name, which is the exact failure the transcript exists to prevent.

| | |
|---|---|
| Decisions | 22,445 |
| Made by an agent | 4,594 (20.5%) |
| Fell back to Forge | 17,851 (79.5%) |
| Of those, session limit | 13,598 |

## What was fixed

`SeatExhausted`, a distinct exception from `SeatTimeout`. A timeout costs one
decision, exhaustion costs every remaining one, and the two must not be handled
the same way.

- `SdkSeat` recognises a quota or refusal reply and raises it, closing itself so
  the next decision fails immediately rather than paying another round trip.
- `MatchServer` records a `seat_exhausted` event, marks the match `exhausted`,
  and ends it.
- `run_sweep` cancels every pending pairing the moment one comes back exhausted.
- `format_table` prints a warning above any result that contains one.

A run can still be cut short by a session limit. It can no longer pretend it
was not.
