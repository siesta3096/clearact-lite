class ClearActError(Exception):
    """Expected, user-visible runtime failure."""


class ProtocolError(ClearActError):
    """A model or persisted message violates the shared protocol."""


class ContextLimit(ClearActError):
    """Required context cannot fit; preserve history and stop."""


class WorkspaceConflict(ClearActError):
    """A file changed after the recorded operation."""


class RecoveryExhausted(ClearActError):
    """Bounded diagnosis could not make progress; preserve a resumable checkpoint."""
