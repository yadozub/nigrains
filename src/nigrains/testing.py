"""The adversarial scenarios, packaged, so your grain meets them in a test.

**Every real defect this package has had was found the same way.** A waiter
that gave up poisoned the activation everybody else was waiting for. A sweep
collected a grain that had not finished activating. A shutdown ran under a
live call. None of those was specific to the grain that found them - anybody
who writes a grain meets the same conditions, and nobody thinks to write the
test.

So the scenarios ship. Hand this your grain class and one representative
call, and it runs them::

    import pytest
    from nigrains.testing import check_conformance

    async def test_my_grain_behaves():
        await check_conformance(
            PriceGrain,
            lambda ref: ref.price_of("AAPL"),
        )

**What it checks is your grain, not this runtime.** The runtime's own
guarantees have their own tests. What cannot be tested there is whether
*your* ``activate`` rebuilds everything your methods need, whether your
answer survives being deactivated and built again, and whether two
activations of one identity - which a cluster allows - agree.

**The call you hand it must be a pure read that returns the same answer
twice.** Half of what is below compares one answer against another, and a
counter that increments will fail every one of them, correctly and
uselessly. If your grain has no pure read, this kit has little to tell you.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from nigrains.call import deadline
from nigrains.errors import GrainError
from nigrains.grain import Grain, GrainId
from nigrains.runtime import Runtime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

G = TypeVar("G", bound=Grain)

_KEY = "conformance"
_CALLERS = 8


class ConformanceFailure(GrainError, AssertionError):  # noqa: N818 - named after
    # AssertionError, which a test runner already knows how to report and which
    # does not carry the suffix either
    """A grain did not behave the way the scenarios require.

    An ``AssertionError`` as well, so a test runner reports it the way it
    reports every other failed expectation.

    Attributes:
        scenario: Which one failed, by the name it has in the report.
        detail: What was seen instead.
    """

    def __init__(self, scenario: str, detail: str) -> None:
        """Names the scenario and what went wrong.

        Args:
            scenario: The failing scenario.
            detail: What was observed.
        """
        super().__init__(f"{scenario}: {detail}")
        self.scenario = scenario
        self.detail = detail


@dataclass(slots=True)
class Outcome:
    """What one scenario found.

    Attributes:
        scenario: Its name.
        passed: Whether it holds. A scenario that could not be run counts
            as passing and says so in ``note`` - a kit that failed a grain
            for being unobservable would teach people to stop running it.
        note: What was seen, in a line, whether it passed or not.
    """

    scenario: str
    passed: bool
    note: str


@dataclass(slots=True)
class Report:
    """Everything the battery found.

    Attributes:
        grain: The class that was checked.
        outcomes: One per scenario, in the order they ran.
    """

    grain: str
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def failures(self) -> list[Outcome]:
        """The scenarios that did not hold.

        Returns:
            The failing outcomes, which is empty when all is well.
        """
        return [outcome for outcome in self.outcomes if not outcome.passed]

    def __str__(self) -> str:
        """Renders the whole battery, passes included.

        Returns:
            One line per scenario.
        """
        lines = [f"conformance of {self.grain}"]
        lines += [
            f"  {'ok  ' if outcome.passed else 'FAIL'} {outcome.scenario}: {outcome.note}"
            for outcome in self.outcomes
        ]
        return "\n".join(lines)


class _Overlap:
    """Counts how many calls are inside the grain's own methods at once.

    **This was a filter first, and a filter is the wrong place.** Filters
    wrap dispatch from outside, which is before a serialised grain acquires
    its lock - so eight callers queueing to enter one at a time were counted
    as eight inside, and a correctly serialised grain failed. The test that
    was written to prove the scenario could fail proved instead that it
    always did.

    So the counting happens where the answer is: a subclass of the grain
    whose public coroutine methods are wrapped. Subclassing rather than
    patching the instance, because a grain with ``__slots__`` has nowhere to
    put a patched method.

    Attributes:
        peak: The most calls seen inside at one moment.
    """

    def __init__(self) -> None:
        """Starts at nothing seen."""
        self.peak = 0
        self._inside = 0

    def observe(self, grain: type[Grain]) -> type[Grain]:
        """Returns a subclass that counts its way in and out.

        Args:
            grain: The class to watch.

        Returns:
            A subclass answering the same way and counting as it goes.
        """
        wrapped = {
            name: self._counting(method)
            for name, method in inspect.getmembers(grain, inspect.iscoroutinefunction)
            if not name.startswith("_") and name not in {"activate", "deactivate"}
        }
        return type(f"Observed{grain.__name__}", (grain,), wrapped)

    def _counting(self, method: Any) -> Any:  # noqa: ANN401 - the grain's own signature
        """Wraps one coroutine method in the count.

        Args:
            method: What to wrap.

        Returns:
            The same method, counted.
        """
        watcher = self

        async def counted(inner: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            watcher._inside += 1
            watcher.peak = max(watcher.peak, watcher._inside)
            try:
                return await method(inner, *args, **kwargs)
            finally:
                watcher._inside -= 1

        return counted


async def check_conformance(
    grain: type[G],
    exercise: Callable[[G], Awaitable[Any]],
    *,
    factory: Callable[[GrainId], Grain] | None = None,
    key: str = _KEY,
    callers: int = _CALLERS,
) -> Report:
    """Runs the battery against one grain class.

    Args:
        grain: The class to check.
        exercise: One representative call, given a reference to the grain.
            **Must be a pure read**: several scenarios compare one answer
            against another.
        factory: How to build the grain, when its constructor wants more
            than an identity.
        key: The identity to use. Any key will do; it is named only so that
            a grain which cares can be given one it likes.
        callers: Concurrent callers in the scenarios that need a crowd.

    Returns:
        The report, whether or not everything held.

    Raises:
        ConformanceFailure: Something did not hold. The report is in the
            message, so a test that simply awaits this gets a useful
            failure without writing any assertions.
    """
    report = Report(grain=grain.__name__)
    for scenario in (
        _one_activation_under_a_herd,
        _the_answer_survives_being_rebuilt,
        _reentrancy_is_what_the_class_says,
        _a_spent_deadline_builds_nothing,
        _two_activations_agree,
    ):
        report.outcomes.append(await scenario(grain, exercise, factory, key, callers))

    if report.failures:
        first = report.failures[0]
        raise ConformanceFailure(first.scenario, f"{first.note}\n\n{report}")
    return report


def _runtime(
    grain: type[Grain],
    factory: Callable[[GrainId], Grain] | None,
    **kwargs: Any,  # noqa: ANN401 - forwarded to Runtime, whose types are its own
) -> Runtime:
    """Builds a runtime holding one kind of grain and nothing else.

    Args:
        grain: The class to register.
        factory: How to build it, or None for the class itself.
        **kwargs: Passed to the runtime.

    Returns:
        The runtime, not yet entered.
    """
    built = Runtime(idle_seconds=kwargs.pop("idle_seconds", 1e9), sweep_seconds=1e9, **kwargs)
    built.register(grain, factory)
    return built


async def _one_activation_under_a_herd(
    grain: type[Any],
    exercise: Callable[[Any], Awaitable[Any]],
    factory: Callable[[GrainId], Grain] | None,
    key: str,
    callers: int,
) -> Outcome:
    """A crowd meeting a cold grain builds it once, not once each."""
    runtime = _runtime(grain, factory)
    reference = runtime.reference(grain, key)
    await asyncio.gather(*(exercise(reference) for _ in range(callers)))

    built = runtime.stats.activations
    return Outcome(
        "one activation under a herd",
        built == 1,
        f"{callers} callers built {built} activation(s)",
    )


async def _the_answer_survives_being_rebuilt(
    grain: type[Any],
    exercise: Callable[[Any], Awaitable[Any]],
    factory: Callable[[GrainId], Grain] | None,
    key: str,
    callers: int,
) -> Outcome:
    """Deactivating and building again gives the same answer.

    The scenario that catches an ``activate`` which does not rebuild
    everything the methods need - the commonest way a grain is wrong, and
    invisible until the first time one is collected in production.
    """
    # Everything is idle the moment it exists, and the sweeper is off, so
    # the one collection this scenario asks for is the only one there is.
    runtime = _runtime(grain, factory, idle_seconds=-1.0)
    reference = runtime.reference(grain, key)
    before = await exercise(reference)

    collected = await runtime.collect()
    after = await exercise(reference)

    if collected == 0:
        return Outcome(
            "the answer survives being rebuilt",
            True,
            "nothing was deactivated, so nothing was rebuilt - not checked",
        )
    return Outcome(
        "the answer survives being rebuilt",
        before == after,
        f"before {before!r}, after {after!r}",
    )


async def _reentrancy_is_what_the_class_says(
    grain: type[Any],
    exercise: Callable[[Any], Awaitable[Any]],
    factory: Callable[[GrainId], Grain] | None,
    key: str,
    callers: int,
) -> Outcome:
    """A grain that did not ask for overlap does not get it.

    **Only one direction of this is checkable.** A grain that says it is
    reentrant may still see no overlap, because a fast call can finish
    before the next one starts - so observing none proves nothing and is
    reported rather than failed. A grain that says it is *not* reentrant
    and is entered twice is a defect with no other reading.
    """
    if factory is not None:
        return Outcome(
            "reentrancy is what the class says",
            True,
            "a factory builds the grain, so this kit cannot watch inside it - not checked",
        )

    watcher = _Overlap()
    observed = watcher.observe(grain)
    runtime = _runtime(observed, None)
    reference = runtime.reference(observed, key)
    await asyncio.gather(*(exercise(reference) for _ in range(callers)))

    if grain.reentrant:
        return Outcome(
            "reentrancy is what the class says",
            True,
            f"declared reentrant; {watcher.peak} caller(s) seen inside at once"
            + ("" if watcher.peak > 1 else " - too fast to overlap, which proves nothing"),
        )
    return Outcome(
        "reentrancy is what the class says",
        watcher.peak <= 1,
        f"declared serialised; {watcher.peak} caller(s) seen inside at once",
    )


async def _a_spent_deadline_builds_nothing(
    grain: type[Any],
    exercise: Callable[[Any], Awaitable[Any]],
    factory: Callable[[GrainId], Grain] | None,
    key: str,
    callers: int,
) -> Outcome:
    """Work that cannot be done does not activate anything to not do it."""
    runtime = _runtime(grain, factory)
    reference = runtime.reference(grain, key)

    refused = False
    with deadline(-1.0):
        try:
            await exercise(reference)
        except TimeoutError:
            refused = True

    activated = runtime.stats.activations
    return Outcome(
        "a spent deadline builds nothing",
        refused and activated == 0,
        f"{'refused' if refused else 'ran anyway'}, {activated} activation(s)",
    )


async def _two_activations_agree(
    grain: type[Any],
    exercise: Callable[[Any], Awaitable[Any]],
    factory: Callable[[GrainId], Grain] | None,
    key: str,
    callers: int,
) -> Outcome:
    """Two activations of one identity answer the same, when they may exist.

    A cluster admits this under a partition, and a grain says whether it can
    survive it. For a grain that says no, the scenario is skipped rather
    than failed: the answer is allowed to differ because the grain is never
    supposed to be in that position, and checking it would fail a grain for
    being what it says it is.
    """
    if not grain.tolerates_double_activation:
        return Outcome(
            "two activations agree",
            True,
            "the class does not claim to tolerate two, so this is not checked",
        )

    first, second = _runtime(grain, factory), _runtime(grain, factory)
    answers = await asyncio.gather(
        exercise(first.reference(grain, key)),
        exercise(second.reference(grain, key)),
    )
    return Outcome(
        "two activations agree",
        answers[0] == answers[1],
        f"{answers[0]!r} against {answers[1]!r}",
    )
