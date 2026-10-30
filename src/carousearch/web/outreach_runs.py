"""Outreach batches in the web app: each Send press starts one batch, sent in a worker thread (Playwright's sync
API and the waits between sends must stay off the event loop), and the page polls its progress.

- OutreachBatch: one batch's progress events, its report or error, and the event that stops it.
- OutreachBatchRegistry: the newest few batches, in memory; at most one sends at a time.
- start_send_batch: approve and send in a worker thread; the sleep between sends ends early when the batch is
  stopped (the Stop button, or the app shutting down), and the rest stay approved.

Pure: the bookkeeping. I/O: start_send_batch's task, through OutreachService.
"""

import asyncio
import logging
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from carousearch.outreach.models import SendReport
from carousearch.outreach.sender import ProgressEvent
from carousearch.outreach.service import OutreachService

logger = logging.getLogger(__name__)

MAX_KEPT_BATCHES = 10
SHUTDOWN_GRACE_SECONDS = 5.0
SEND_FAILED_TEXT = "The batch failed. The terminal running carousearch web has the details."
STOPPED_TEXT = "Stopped before the next message; the rest stay approved for a later batch."


class BatchStopped(Exception):
    """Raised inside the send thread's sleep when the batch is stopped."""


@dataclass
class OutreachBatch:
    batch_id: str
    run_id: str
    dry_run: bool
    started_at: datetime
    events: list[ProgressEvent] = field(default_factory=list)  # appended by the send thread
    report: SendReport | None = None
    error: str | None = None
    stop_requested: threading.Event = field(default_factory=threading.Event)

    def record(self, event: ProgressEvent) -> None:
        self.events.append(event)

    @property
    def running(self) -> bool:
        return self.report is None and self.error is None

    def interruptible_sleep(self) -> Callable[[float], None]:
        def sleep(seconds: float) -> None:
            if self.stop_requested.wait(seconds):
                raise BatchStopped

        return sleep


class OutreachBatchRegistry:
    def __init__(self, limit: int = MAX_KEPT_BATCHES) -> None:
        self._limit = limit
        self._batches: OrderedDict[str, OutreachBatch] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, run_id: str, dry_run: bool, now: datetime) -> OutreachBatch:
        if self.active() is not None:
            raise RuntimeError("a batch is already being sent")
        batch = OutreachBatch(batch_id=secrets.token_urlsafe(8), run_id=run_id, dry_run=dry_run, started_at=now)
        self._batches[batch.batch_id] = batch
        while len(self._batches) > self._limit:
            self._batches.popitem(last=False)
        return batch

    def get(self, batch_id: str) -> OutreachBatch | None:
        return self._batches.get(batch_id)

    def active(self) -> OutreachBatch | None:
        return next((batch for batch in self._batches.values() if batch.running), None)

    def keep_task(self, task: "asyncio.Task[None]") -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self, grace_seconds: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Ask the batch in flight to stop at its next wait, and give it a moment (the app is shutting down)."""
        for batch in self._batches.values():
            batch.stop_requested.set()
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=grace_seconds)


def start_send_batch(
    registry: OutreachBatchRegistry,
    service: OutreachService,
    run_id: str,
    message_ids: Sequence[int],
    dry_run: bool,
    now: datetime,
) -> OutreachBatch:
    """Record a batch and start sending it in a worker thread; returns at once."""
    batch = registry.start(run_id, dry_run, now)
    registry.keep_task(asyncio.create_task(send_in_thread(batch, service, list(message_ids)), name=f"outreach-{run_id}"))
    return batch


async def send_in_thread(batch: OutreachBatch, service: OutreachService, message_ids: list[int]) -> None:
    try:
        batch.report = await asyncio.to_thread(
            service.approve_and_send, batch.run_id, message_ids, batch.dry_run, batch.record,
            batch.interruptible_sleep(),
        )
    except BatchStopped:
        logger.warning("Outreach batch %s stopped", batch.batch_id)
        batch.error = STOPPED_TEXT
    except Exception:  # never a silent death: the batch says it failed and the log has the traceback
        logger.exception("Outreach batch %s failed", batch.batch_id)
        batch.error = SEND_FAILED_TEXT
