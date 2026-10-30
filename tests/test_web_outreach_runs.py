"""The web app's outreach batch registry and the stoppable wait between sends."""

import asyncio

import pytest

from carousearch.outreach.models import SendReport
from carousearch.web.outreach_runs import BatchStopped, OutreachBatchRegistry

from outreach_examples import START


def test_one_batch_at_a_time_and_only_the_newest_few_are_kept() -> None:
    registry = OutreachBatchRegistry(limit=2)
    first = registry.start("run", dry_run=True, now=START)

    with pytest.raises(RuntimeError):
        registry.start("run", dry_run=False, now=START)
    first.report = SendReport()
    second = registry.start("run", dry_run=False, now=START)
    second.error = "stopped"
    third = registry.start("run", dry_run=False, now=START)

    assert registry.active() is third and registry.get(first.batch_id) is None
    assert registry.get(second.batch_id) is second


def test_the_wait_between_sends_ends_early_when_the_batch_is_stopped() -> None:
    batch = OutreachBatchRegistry().start("run", dry_run=False, now=START)
    sleep = batch.interruptible_sleep()

    sleep(0)  # not stopped: returns after the wait
    batch.stop_requested.set()
    with pytest.raises(BatchStopped):
        sleep(60)  # returns at once


def test_stopping_the_registry_asks_every_batch_to_stop() -> None:
    registry = OutreachBatchRegistry()
    batch = registry.start("run", dry_run=False, now=START)

    asyncio.run(registry.stop(grace_seconds=0))

    assert batch.stop_requested.is_set()
