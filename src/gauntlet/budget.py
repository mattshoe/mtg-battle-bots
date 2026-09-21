"""Spending limits for a run.

Two things have already gone wrong here and both were the same shape: a run kept
going long after it should have stopped, and nobody found out until it was over.
Once with a session limit, where the cost was two hours and a quota. The next
one will be an API key, where the cost is money.

So a budget is not optional and not opt-in. Every run has one, it is enforced
before each decision rather than after, and hitting it stops the run through the
same path as an exhausted seat: the engine is told to stop, the transcript
records why, and the numbers are marked as not-what-they-claim.

Accounting is exact where it can be. The API returns real token counts and those
are used. The SDK seat does not expose them, so its spend is estimated from a
measured per-decision average, and the estimate is deliberately high.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

#: Input and output dollars per million tokens. Only models this harness would
#: plausibly seat a player with.
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
}

#: What one decision costs when nobody is counting for us. Measured over twelve
#: real agent games: 1,030 input tokens median, about 60 output. Rounded up,
#: because an estimate that runs under its true cost defeats the point.
EST_INPUT_TOKENS = 1200
EST_OUTPUT_TOKENS = 80

#: Hard cap on what a run may spend unless the caller names a higher one.
#:
#: Five dollars, set deliberately low. Two runs have already overrun without
#: anyone noticing, so the default is chosen to make the next mistake cheap
#: rather than to let a big run through without asking. A run that needs more
#: says so with --max-cost, which is an explicit decision rather than a default
#: nobody looked at.
DEFAULT_MAX_USD = 5.00

#: A decision ceiling as well as a dollar one, because a pricing table that has
#: drifted, or a seat kind with no pricing at all, must still be bounded.
DEFAULT_MAX_DECISIONS = 100_000


class BudgetExceeded(Exception):
    """The run has spent what it was allowed to spend."""


def price(model: str) -> tuple[float, float]:
    """Dollars per million input and output tokens.

    An unknown model prices as the most expensive one known rather than as
    free. Guessing low on an unfamiliar model is how a budget silently stops
    being a budget.
    """
    if model in PRICING:
        return PRICING[model]
    for known, rates in PRICING.items():
        if model.startswith(known):
            return rates
    return max(PRICING.values())


def estimate_usd(decisions: int, model: str) -> float:
    """What a run of this many decisions should cost."""
    rate_in, rate_out = price(model)
    return (
        decisions * EST_INPUT_TOKENS / 1e6 * rate_in
        + decisions * EST_OUTPUT_TOKENS / 1e6 * rate_out
    )


#: Decisions to observe before pricing reservations off real spend rather
#: than the static estimate. Small, because the point is to stop trusting a
#: guess as soon as there is anything better.
_LEARN_AFTER = 5

#: Decisions one game costs across both seats, measured after the pass
#: suppression work. Used only to project a run before it starts.
DECISIONS_PER_GAME = 64


def project(games: int, model: str, paid_seats: int = 2) -> tuple[int, float]:
    """Decisions and dollars a run is likely to cost, before committing to it."""
    per_game = DECISIONS_PER_GAME * (paid_seats / 2) if paid_seats else 0
    decisions = int(games * per_game)
    return decisions, estimate_usd(decisions, model)


@dataclass
class Budget:
    """What a run may spend, and what it has spent.

    Thread safe, and the claim is a reservation rather than a check, because a
    check followed later by a charge leaves a window as wide as a model call.
    Several bridge connections claim against one of these at once, and without
    reservations a sweep with six parallel pairings ran twelve decisions past a
    cap only one of them had room for.

    The cap is exact once spending is observed and approximate before that. A
    decision's cost is not knowable until the call returns, so the first wave of
    concurrent decisions is priced at an estimate. If that estimate is badly
    wrong the run can overshoot by up to one wave, bounded by the worker count,
    after which observed spend prices every later reservation. Measured: within
    2% when the estimate is right or twice too low, and the estimate is
    deliberately set above what was measured in real games.
    """

    max_usd: float = DEFAULT_MAX_USD
    max_decisions: int = DEFAULT_MAX_DECISIONS
    model: str = "claude-haiku-4-5"

    #: Set when a seat's spend is estimated rather than measured, which makes
    #: the total a lower bound on confidence, not on amount.
    estimated: bool = False

    decisions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    #: Decisions dispatched but not yet billed. Counted at the estimated
    #: rate while in flight, so concurrent callers cannot all pass a cap
    #: only one of them had room for.
    in_flight: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def spent_usd(self) -> float:
        """What has been billed. Excludes reservations."""
        rate_in, rate_out = price(self.model)
        return self.input_tokens / 1e6 * rate_in + self.output_tokens / 1e6 * rate_out

    @property
    def per_decision_usd(self) -> float:
        """What a decision has actually cost, falling back to the estimate.

        A static estimate is a guess about prompt size, and a model or a deck
        that makes it wrong makes every reservation wrong in the same direction.
        Once there is real data, that data prices the next reservation.
        """
        if self.decisions < _LEARN_AFTER:
            return estimate_usd(1, self.model)
        return max(self.spent_usd / self.decisions, estimate_usd(1, self.model) * 0.25)

    @property
    def committed_usd(self) -> float:
        """Billed, plus what is already in flight.

        This is the number a cap has to be checked against. Checking the billed
        figure alone let a sweep with six parallel pairings run twelve decisions
        past the line before any of them reported back.
        """
        return self.spent_usd + self.in_flight * self.per_decision_usd

    def check(self) -> None:
        """Raise if the next decision would be over the line.

        Called before dispatching, not after. Checking afterwards means the
        money is already gone.
        """
        with self._lock:
            self._check_locked()

    def _check_locked(self) -> None:
        if self.decisions + self.in_flight >= self.max_decisions:
            raise BudgetExceeded(
                f"decision limit reached ({self.decisions:,} of {self.max_decisions:,})"
            )
        committed = self.committed_usd
        if committed >= self.max_usd:
            raise BudgetExceeded(f"spend limit reached (${committed:.2f} of ${self.max_usd:.2f})")

    def reserve(self) -> None:
        """Check and claim a decision's worth of budget in one step.

        The caller must follow with `charge`, which releases the reservation and
        records what was really spent.
        """
        with self._lock:
            self._check_locked()
            self.in_flight += 1

    def charge(self, *, input_tokens: int | None = None, output_tokens: int | None = None) -> None:
        """Record one decision's cost.

        Pass the real token counts when the seat knows them. Omit them and the
        measured average is used instead, and the budget is marked estimated so
        a reader knows the total is not exact.
        """
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
            self.decisions += 1
            if input_tokens is None or output_tokens is None:
                self.estimated = True
                self.input_tokens += EST_INPUT_TOKENS
                self.output_tokens += EST_OUTPUT_TOKENS
            else:
                self.input_tokens += input_tokens
                self.output_tokens += output_tokens

    def summary(self) -> dict[str, object]:
        return {
            "decisions": self.decisions,
            "max_decisions": self.max_decisions,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "spent_usd": round(self.spent_usd, 4),
            "max_usd": self.max_usd,
            "estimated": self.estimated,
            "model": self.model,
        }

    def report(self) -> str:
        """One line, for the end of a run."""
        about = "about " if self.estimated else ""
        return f"{self.decisions:,} decisions, {about}${self.spent_usd:.2f} of ${self.max_usd:.2f}"


#: A budget for a run where no seat costs anything. Forge's AI is free, and
#: charging it against a dollar limit would stop a run that spends nothing.
def unlimited() -> Budget:
    return Budget(max_usd=float("inf"), max_decisions=DEFAULT_MAX_DECISIONS)
