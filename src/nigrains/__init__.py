"""Virtual actors for asyncio.

A grain has an identity rather than a lifetime. Application code never
creates one and never destroys one: it calls one, and the runtime decides
whether an activation has to be built first and when an idle one goes away.

    runtime = Runtime()
    runtime.register(Counter)
    async with runtime:
        await runtime.reference(Counter, "a").increment()

See :class:`~nigrains.runtime.Runtime` for what this node-local half does
and, more importantly, what it does not.

To my teacher and mentor, Vitaly Chashin.
"""

import logging

from nigrains.call import Call, CallFilter, DeadlineExceeded, deadline, remaining
from nigrains.cluster import (
    Cluster,
    LoopbackTransport,
    Membership,
    Ring,
    StaticMembership,
    Transport,
    UnreachableNodeError,
)
from nigrains.errors import GrainError, GrainNotRegisteredError, NoSuchGrainMethodError
from nigrains.grain import Grain, GrainId
from nigrains.reminders import (
    GrainReminders,
    InMemoryReminderStore,
    Reminder,
    ReminderStore,
)
from nigrains.runtime import Runtime, Stats
from nigrains.state import (
    ConcurrentChange,
    GrainState,
    InMemoryStateStore,
    StateStore,
    Stored,
)

# So that a host which configured no logging sees no "no handlers" noise,
# and one which did sees everything. The library's own level is left
# alone: choosing it is the host's business.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "Call",
    "CallFilter",
    "Cluster",
    "ConcurrentChange",
    "DeadlineExceeded",
    "Grain",
    "GrainError",
    "GrainId",
    "GrainNotRegisteredError",
    "GrainReminders",
    "GrainState",
    "InMemoryReminderStore",
    "InMemoryStateStore",
    "LoopbackTransport",
    "Membership",
    "NoSuchGrainMethodError",
    "Reminder",
    "ReminderStore",
    "Ring",
    "Runtime",
    "StateStore",
    "StaticMembership",
    "Stats",
    "Stored",
    "Transport",
    "UnreachableNodeError",
    "deadline",
    "remaining",
]
