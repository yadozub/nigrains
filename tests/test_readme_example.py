"""The example in the README, run.

A README is documentation that rots silently: it is the first thing a reader
tries and the last thing anybody edits when the API moves. This is that
example, verbatim, so moving the API breaks a test rather than a stranger's
first five minutes.
"""

from __future__ import annotations

from nigrains import Grain, GrainId, Runtime


class Counter(Grain):
    async def activate(self) -> None:
        self.count = 0

    async def increment(self) -> int:
        self.count += 1
        return self.count


async def test_the_readme_example_does_what_it_says() -> None:
    runtime = Runtime()
    runtime.register("counter", Counter)

    async with runtime:
        assert await runtime.call(GrainId("counter", "a"), "increment") == 1
        assert await runtime.call(GrainId("counter", "a"), "increment") == 2
        assert await runtime.call(GrainId("counter", "b"), "increment") == 1
