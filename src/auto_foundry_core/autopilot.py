"""Small local event loop for continuously dispatching planner actions."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event
import time
from typing import Any, Callable

from .lifecycle import RunLifecycle
from .requirement_planning import PlannerAction, RequirementSupervisorWorkspace
from .workspace import RunContext


ActionDispatcher = Callable[[PlannerAction], Any]


@dataclass(frozen=True)
class AutopilotTick:
    """One observable local scheduling pass."""

    status: str
    lifecycle_state: str
    actions: tuple[PlannerAction, ...] = ()
    results: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "lifecycle_state": self.lifecycle_state,
            "actions": [action.to_dict() for action in self.actions],
            "results": list(self.results),
        }


class LocalRunAutopilot:
    """Continuously ask Planner for work and hand it to a local dispatcher.

    The event loop deliberately owns no analytical policy.  Agents remain
    free to interpret and execute each typed :class:`PlannerAction`; after a
    dispatch, durable state is simply reloaded.  A pause stops dispatching,
    and a later resume, requirement revision, or reopened completed run is
    noticed on the next tick without a migration or version check.
    """

    def __init__(self, context: RunContext) -> None:
        if not isinstance(context, RunContext):
            raise TypeError("LocalRunAutopilot requires a RunContext")
        self.context = context

    def tick(self, dispatcher: ActionDispatcher | None = None) -> AutopilotTick:
        lifecycle = RunLifecycle.load(self.context)
        if lifecycle.paused:
            return AutopilotTick("paused", lifecycle.state)

        workspace = RequirementSupervisorWorkspace(self.context)
        actions = workspace.next_actions()
        if not actions:
            lifecycle.reconcile_from_run()
            lifecycle = RunLifecycle.load(self.context)
            actions = workspace.next_actions() if not lifecycle.paused else ()
        if not actions:
            status = "paused" if lifecycle.paused else "idle"
            return AutopilotTick(status, lifecycle.state)
        if dispatcher is None:
            return AutopilotTick("ready", lifecycle.state, actions)

        results = tuple(dispatcher(action) for action in actions)
        refreshed = RunLifecycle.load(self.context)
        return AutopilotTick("dispatched", refreshed.state, actions, results)

    def run(
        self,
        dispatcher: ActionDispatcher,
        *,
        interval_seconds: float = 1.0,
        stop_event: Event | None = None,
        on_tick: Callable[[AutopilotTick], Any] | None = None,
    ) -> None:
        """Run until the caller sets ``stop_event`` or interrupts the process.

        Idle and completed runs remain observable instead of terminating: a
        requirement added later is therefore picked up automatically.
        """

        if interval_seconds < 0:
            raise ValueError("interval_seconds must be nonnegative")
        event = stop_event or Event()
        while not event.is_set():
            tick = self.tick(dispatcher)
            if on_tick is not None:
                on_tick(tick)
            if interval_seconds:
                event.wait(interval_seconds)
            else:
                time.sleep(0)


__all__ = ["ActionDispatcher", "AutopilotTick", "LocalRunAutopilot"]
