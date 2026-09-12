"""The conformance kit, checked against grains that pass and grains that do not.

A kit that only ever passes proves nothing, so every scenario here has a
grain written to break it. Those grains are the documentation of what each
scenario is for.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from nigrains import Grain
from nigrains.testing import ConformanceFailure, check_conformance


class WellBehaved(Grain):
    """A cache in front of something that does not change."""

    grain_type = "well_behaved"
    reentrant = True
    tolerates_double_activation = True

    async def activate(self) -> None:
        self.answer = "forty-two"

    async def look_up(self) -> str:
        await asyncio.sleep(0)
        return self.answer


class ForgetsToRebuild(Grain):
    """Builds its answer once, in a place activation does not reach.

    The commonest way a grain is wrong, and invisible until the first time
    one is collected in production.
    """

    grain_type = "forgets"
    tolerates_double_activation = True
    _built: ClassVar[dict[str, int]] = {}

    async def activate(self) -> None:
        self._count = ForgetsToRebuild._built.get(self.id.key, 0) + 1
        ForgetsToRebuild._built[self.id.key] = self._count

    async def look_up(self) -> int:
        return self._count


class Disagrees(Grain):
    """Two activations of it answer differently, and it claims otherwise."""

    grain_type = "disagrees"
    tolerates_double_activation = True
    _next = 0

    async def activate(self) -> None:
        Disagrees._next += 1
        self._mine = Disagrees._next

    async def look_up(self) -> int:
        return self._mine


class LiesAboutOverlap(Grain):
    """Serialised by declaration, and this test makes sure it is by fact."""

    grain_type = "lies_about_overlap"
    reentrant = False

    async def look_up(self) -> str:
        await asyncio.sleep(0)
        return "one"


async def test_a_well_behaved_grain_passes_every_scenario() -> None:
    report = await check_conformance(WellBehaved, lambda ref: ref.look_up())

    assert report.failures == []
    assert len(report.outcomes) == 5


async def test_a_grain_that_does_not_rebuild_is_caught() -> None:
    """The scenario that matters most, on the mistake that is easiest to make."""
    ForgetsToRebuild._built.clear()

    with pytest.raises(ConformanceFailure, match="survives being rebuilt"):
        await check_conformance(ForgetsToRebuild, lambda ref: ref.look_up())


async def test_two_activations_that_disagree_are_caught() -> None:
    """It said it could tolerate two of itself, and it cannot."""
    with pytest.raises(ConformanceFailure, match="two activations agree"):
        await check_conformance(Disagrees, lambda ref: ref.look_up())


async def test_a_serialised_grain_is_confirmed_serialised() -> None:
    """The direction of the reentrancy scenario that can be failed."""
    report = await check_conformance(LiesAboutOverlap, lambda ref: ref.look_up())

    overlap = next(o for o in report.outcomes if "reentrancy" in o.scenario)
    assert overlap.passed
    assert "1 caller(s) seen inside" in overlap.note


async def test_a_grain_that_never_claimed_two_is_not_checked_for_two() -> None:
    """Failing a grain for being what it says it is would teach people to
    stop running this.
    """
    report = await check_conformance(LiesAboutOverlap, lambda ref: ref.look_up())

    two = next(o for o in report.outcomes if o.scenario == "two activations agree")
    assert two.passed
    assert "does not claim" in two.note


async def test_the_report_reads_as_a_report() -> None:
    """It is printed when something fails, so it has to be worth printing."""
    report = await check_conformance(WellBehaved, lambda ref: ref.look_up())

    rendered = str(report)
    assert "conformance of WellBehaved" in rendered
    assert rendered.count("ok  ") == 5


async def test_a_reentrant_grain_reports_what_was_seen_rather_than_guessing() -> None:
    """Observing no overlap proves nothing, and the note says so."""

    class TooFast(Grain):
        grain_type = "too_fast"
        reentrant = True

        async def look_up(self) -> str:
            return "immediately"

    report = await check_conformance(TooFast, lambda ref: ref.look_up())

    overlap = next(o for o in report.outcomes if "reentrancy" in o.scenario)
    assert overlap.passed
    assert "proves nothing" in overlap.note


async def test_a_grain_needing_more_than_an_identity_is_given_a_factory() -> None:
    """Not every grain's constructor wants only its address."""

    class NeedsHelp(Grain):
        grain_type = "needs_help"
        tolerates_double_activation = True

        def __init__(self, grain_id: Any, answer: str) -> None:
            super().__init__(grain_id)
            self._answer = answer

        async def look_up(self) -> str:
            return self._answer

    report = await check_conformance(
        NeedsHelp,
        lambda ref: ref.look_up(),
        factory=lambda grain_id: NeedsHelp(grain_id, "given"),
    )

    assert report.failures == []
