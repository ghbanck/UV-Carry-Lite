"""TransferSession: the aggregate root of one carry.

A session owns one immutable origin: the island before its first move, the context fingerprint and
the image targets known then. It moves between OPEN and TRANSFORMING while the user moves the
island, then through PLANNING and APPLYING to COMMITTED on Ctrl+Enter, or it ends CANCELLED or
FAILED. A command that the current state does not accept raises InvalidTransition and changes
nothing. Every accepted command records a domain event; the events are the session's trace.

The session decides nothing about Blender: the caller observes the context, restores UVs and writes
images, and reports the outcome through these commands.
"""

import time

ALLOWED_TRANSFORMS = frozenset({"TRANSFORM_OT_translate", "TRANSFORM_OT_rotate", "TRANSFORM_OT_resize"})
TERMINAL = frozenset({"COMMITTED", "CANCELLED", "FAILED"})


class InvalidTransition(Exception):
    """A command the session's current state does not accept."""


class TransferSession:
    def __init__(self, session_id, origin, fingerprint, clock=time.perf_counter, **opening):
        self.id = session_id
        self.origin = origin                  # immutable after opening
        self.fingerprint = dict(fingerprint)
        self.state = "OPEN"
        self.active = None                    # the running transform, while TRANSFORMING
        self.history = []                     # confirmed transforms: {"op", "props", "allowed"}
        self.events = []
        self.result = None                    # {"state", "severity", "message"} once terminal
        self._clock = clock
        self._t0 = clock()
        self._record("SessionOpened", fingerprint=self.fingerprint, **opening)

    # -- queries

    @property
    def closed(self):
        return self.state in TERMINAL

    @property
    def disallowed(self):
        """Confirmed operators that a carry may not reproduce, sorted."""
        return sorted({h["op"] for h in self.history if not h["allowed"]})

    def last(self, event):
        """The newest recorded event with this name, or None."""
        return next((e for e in reversed(self.events) if e["event"] == event), None)

    # -- transforms

    def begin_transform(self, op, **data):
        self._require("begin_transform", "OPEN")
        self.state, self.active = "TRANSFORMING", op
        return self._record("TransformStarted", op=op, **data)

    def confirm_transform(self, op, props=None, **data):
        """A transform ended confirmed. From OPEN too: a transform can start and end between two
        observations."""
        self._require("confirm_transform", "OPEN", "TRANSFORMING")
        entry = {"op": op, "props": dict(props or {}), "allowed": op in ALLOWED_TRANSFORMS}
        self.history.append(entry)
        self.state, self.active = "OPEN", None
        return self._record("TransformConfirmed", op=op, allowed=entry["allowed"], props=entry["props"], **data)

    def cancel_transform(self, **data):
        self._require("cancel_transform", "TRANSFORMING")
        op, self.state, self.active = self.active, "OPEN", None
        return self._record("TransformCancelled", op=op, **data)

    # -- commit

    def begin_commit(self):
        """Ctrl+Enter with no transform running: plan the transfer."""
        self._require("begin_commit", "OPEN")
        self.state = "PLANNING"
        return self._record("TransferPlanningStarted", moves=len(self.history))

    def defer(self, reason, **data):
        """Planning stopped before anything was decided (no image to carry into yet); the session
        stays open."""
        self._require("defer", "PLANNING")
        self.state = "OPEN"
        return self._record("CommitDeferred", reason=reason, **data)

    def planned(self, **summary):
        self._require("planned", "PLANNING")
        self.state = "APPLYING"
        return self._record("TransferPlanned", **summary)

    def reject(self, category, message, restore, **detail):
        """Planning refused the transfer; nothing was written."""
        self._require("reject", "PLANNING")
        self._record("TransferRejected", category=category, message=message, restore=restore, detail=detail)
        return self._close("FAILED", "WARNING", message)

    def committed(self, message, **report):
        self._require("committed", "APPLYING")
        self._record("InMemoryCommitSucceeded", **report)
        return self._close("COMMITTED", "INFO", message)

    def commit_failed(self, category, message, rollback, restore, **detail):
        """Publishing failed. `rollback` says whether the writes made were all taken back."""
        self._require("commit_failed", "APPLYING")
        self._record("InMemoryCommitFailed", category=category, message=message, rollback=rollback,
                     restore=restore, detail=detail)
        if rollback not in (None, "complete"):
            self._record("RollbackFailed", rollback=rollback)
        return self._close("FAILED", "ERROR", message)

    # -- endings

    def cancel(self, reason, restore, message):
        self._require("cancel", "OPEN")
        self._record("SessionCancelled", reason=reason, restore=restore)
        return self._close("CANCELLED", "INFO", message)

    def invalidate(self, changed, restore, message):
        """The context no longer matches the fingerprint (ContextChanged)."""
        self._require("invalidate", "OPEN", "TRANSFORMING", "PLANNING")
        self._record("SessionInvalidated", changed=list(changed), restore=restore)
        return self._close("FAILED", "WARNING", message)

    def drop(self, reason, message):
        """End without touching the mesh or the images (file loading, undo, handler removed)."""
        if self.closed:
            raise InvalidTransition(f"drop: session already {self.state}")
        self._record("SessionDropped", reason=reason)
        return self._close("CANCELLED", "WARNING", message)

    def note(self, event, **data):
        """Record an observation that changes no state (input seen, a commit ignored, ...)."""
        return self._record(event, **data)

    # -- internals

    def _require(self, command, *states):
        if self.state not in states:
            raise InvalidTransition(f"{command} in state {self.state}; accepted in {', '.join(states)}")

    def _record(self, event, **data):
        entry = {"t_ms": round((self._clock() - self._t0) * 1000, 1), "event": event}
        entry.update(data)
        self.events.append(entry)
        return entry

    def _close(self, state, severity, message):
        self.state, self.active = state, None
        self.result = {"state": state, "severity": severity, "message": message}
        self._record("SessionClosed", **self.result)
        return self.result
