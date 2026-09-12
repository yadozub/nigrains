"""What a grain is, and the two things a grain declares about itself.

A grain is a virtual actor: it has an identity rather than a lifetime, and
the runtime decides when an activation of it exists. Application code never
creates one and never destroys one - it calls one, and the call is what
brings it into being.

**Nothing here knows what a grain is for.** This package has no
dependencies at all, which is what lets it be used by something other than
the system it was written for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class GrainId:
    """Which grain, of which kind.

    The identity is the address. There is no separate handle, no creation
    step and nothing to look up before calling: a grain of a given type and
    key is always callable, and whether an activation of it exists at this
    moment is the runtime's business and not the caller's.

    Attributes:
        type: The registered kind, naming the behaviour.
        key: Which one of that kind. A string because it has to survive a
            wire eventually, and because a runtime that accepted arbitrary
            objects here would have to decide how two of them compare.
    """

    type: str
    key: str

    def __str__(self) -> str:
        """Renders the identity the way logs and directory keys spell it.

        Returns:
            ``type/key``.
        """
        return f"{self.type}/{self.key}"


class Grain:
    """Base class for a grain's behaviour.

    Subclasses add methods; those methods are what a caller invokes by name.
    Two hooks bracket an activation's life, and both have a default that
    does nothing, because most grains need neither.

    **Reentrancy is declared here and nowhere else.** The virtual actor
    model's default is one message at a time, which is what makes a grain's
    state safe without a lock anywhere in the application. That default is
    wrong for a grain whose methods only read: serialising pure reads would
    queue every concurrent caller behind one, which is the opposite of why
    the fleet exists. So a grain says which it is, and says it as a property
    of the class, where it is read once rather than argued about per call.

    A reentrant grain **must not** mutate state in a way another call could
    observe half-done. That is not checked and cannot be; it is the whole
    of what the flag means, and a grain that breaks it has no protection.

    Attributes:
        id: This activation's identity.
        grain_type: The kind this class answers for - the first half of
            every identity addressed to it, and a **wire identifier**: it
            travels in a message and, once there is a cluster, between
            machines. Declared rather than derived from the class name,
            because renaming a class is a refactor and renaming an address
            is a migration, and a default would make them the same
            keystroke.
        reentrant: Whether calls may overlap. Default False, matching the
            model rather than matching our commonest grain.
        tolerates_double_activation: Whether two activations of this
            identity, in two processes at once, are harmless. **Default
            False, which is the safe answer and the wrong one for a cache.**

            A cluster cannot promise otherwise: two halves of a partition
            each believe they own a share and each is right about its own
            half. A grain fronting something immutable is unaffected - two
            of it are two caches answering the same - and says so. A grain
            whose state is the truth must not, and a runtime asked to
            cluster one will refuse rather than let the first split find
            out.

            Read today by the conformance kit, which skips the scenario
            comparing two activations for a grain that never claimed to
            survive them.
    """

    grain_type: ClassVar[str] = ""
    reentrant: ClassVar[bool] = False
    tolerates_double_activation: ClassVar[bool] = False

    def __init__(self, grain_id: GrainId) -> None:
        """Binds the activation to its identity.

        Args:
            grain_id: Which grain this is an activation of.
        """
        self.id = grain_id

    async def activate(self) -> None:
        """Prepares the activation before its first call is served.

        Called once, with every caller of this grain waiting on it. Whatever
        a grain needs in order to answer - an index read from a row store, a
        connection, a warmed cache - is read here. A failure propagates to
        every waiting caller and leaves nothing activated, so the next call
        tries again rather than meeting a half-built grain.
        """

    async def deactivate(self) -> None:
        """Releases whatever the activation held.

        Called when the runtime collects an idle grain, and on shutdown.
        **Not a save hook**: this package has no persistence concept, and a
        grain whose state has to survive is a grain that wrote it somewhere
        already. A failure here is logged and does not stop the collection -
        the activation is going away either way.
        """
