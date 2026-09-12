"""The example in the README, run.

A README is documentation that rots silently: it is the first thing a reader
tries and the last thing anybody edits when the API moves. This is that
example, verbatim, so moving the API breaks a test rather than a stranger's
first five minutes.
"""

from __future__ import annotations

from nigrains import Grain, Runtime


class Counter(Grain):
    grain_type = "counter"

    async def activate(self) -> None:
        self.count = 0

    async def increment(self) -> int:
        self.count += 1
        return self.count


async def test_the_readme_example_does_what_it_says() -> None:
    runtime = Runtime()
    runtime.register(Counter)

    async with runtime:
        first = runtime.reference(Counter, "a")
        assert await first.increment() == 1
        assert await first.increment() == 2
        assert await runtime.reference(Counter, "b").increment() == 1
