"""
eld_scheduler.py

Central, rate-limited scheduler for every outbound Factor/Leader ELD
request, across every team and every loop (dispatch, FMCSA, onboarding
validation) in this process. Replaces the old design (a single global
threading.Lock + fixed sleep buried inside eld_factor._request_with_retries)
with an explicit queue + worker pool, for two reasons:

1. Observability - the old lock was invisible: no way to see how many
   requests were waiting, how many had failed, or how many had been
   rate-limited, short of grepping raw logs. This tracks all of that.
2. A path to safely raising throughput later - concurrency here is one
   number (max_concurrency), not something to reason about by re-deriving
   which threads currently hold which lock. Raising it is a config change,
   not a rewrite.

Deliberately NOT a rewrite of eld_factor.py's actual request/retry logic
(status code interpretation, 401/429 handling, exponential backoff) - that
all still lives there, unchanged. This module owns exactly one thing: given
a job (a plain callable), run it no sooner than min_gap_seconds after the
last one finished, with at most max_concurrency running at once, on a
single shared queue every caller feeds into.

max_concurrency defaults to 1 - this is the FIRST safe step (2026-09-22) of
a larger plan (see the "Teams -> central job queue -> rate-limited
scheduler -> ELD workers" design discussed with the user): prove this
refactor is behavior-neutral (same effective serialization as the old lock,
same gap) before ever touching concurrency itself. Only after that's
confirmed solid does raising max_concurrency (still just a number here, not
a code change) become the next, separately-tested step.

Egress-path support (multiple outbound IPs) is deliberately NOT built yet -
it's out of scope for this first step. The one thing designed in from the
start so it doesn't require a rewrite later: team-to-path assignment must
never become business logic (see the user's explicit requirement) - so
when paths are added, they belong here, as an internal scheduling detail,
never in eld_factor.py/sync.py/multi_sync.py's per-team code.
"""

import queue
import threading
import time
from concurrent.futures import Future


class EldRequestScheduler:
    """One shared queue + worker pool. Call submit(fn, *args, **kwargs) to
    get a concurrent.futures.Future back - call .result() on it to block
    until that job has actually run and get its return value (or have it
    re-raise fn's own exception, exactly like calling fn() directly would).

    Every caller across the whole process should share ONE instance (see
    the module-level `default_scheduler` below) - creating a second
    instance would just recreate the old bug this replaces (two things
    independently deciding it's safe to send a request "now")."""

    def __init__(self, max_concurrency=1, min_gap_seconds=0.5):
        self.max_concurrency = max_concurrency
        self.min_gap_seconds = min_gap_seconds
        self._queue = queue.Queue()
        self._state_lock = threading.Lock()
        self._last_dispatch_at = 0.0
        self.stats = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "total_duration_seconds": 0.0,
        }
        self._workers = [
            threading.Thread(target=self._worker_loop, name=f"eld-worker-{i}", daemon=True)
            for i in range(max_concurrency)
        ]
        for worker in self._workers:
            worker.start()

    def submit(self, fn, *args, **kwargs):
        future = Future()
        with self._state_lock:
            self.stats["submitted"] += 1
        self._queue.put((fn, args, kwargs, future))
        return future

    def queue_depth(self):
        """How many jobs are currently waiting for a free worker - not
        counting whichever job(s) a worker is actively running right now."""
        return self._queue.qsize()

    def get_stats(self):
        """A snapshot dict: submitted/completed/failed counts, average job
        duration, and current queue depth. Read-only, safe to call from any
        thread at any time - e.g. a future control_bot /status command."""
        with self._state_lock:
            snapshot = dict(self.stats)
        completed = snapshot["completed"] or 1  # avoid a divide-by-zero on a fresh scheduler
        snapshot["avg_duration_seconds"] = snapshot["total_duration_seconds"] / completed
        snapshot["queue_depth"] = self.queue_depth()
        snapshot["max_concurrency"] = self.max_concurrency
        snapshot["min_gap_seconds"] = self.min_gap_seconds
        return snapshot

    def _worker_loop(self):
        while True:
            fn, args, kwargs, future = self._queue.get()
            with self._state_lock:
                wait = self.min_gap_seconds - (time.time() - self._last_dispatch_at)
            if wait > 0:
                time.sleep(wait)

            start = time.time()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - deliberately broad, see below
                # Any exception fn raises (a real HTTP error, a timeout,
                # whatever) is handed back to whoever called .result() on
                # this future, exactly as if they'd called fn() themselves
                # inline - this scheduler only controls WHEN fn runs, never
                # swallows or reinterprets what it does.
                with self._state_lock:
                    self._last_dispatch_at = time.time()
                    self.stats["failed"] += 1
                    self.stats["total_duration_seconds"] += time.time() - start
                future.set_exception(exc)
                continue

            with self._state_lock:
                self._last_dispatch_at = time.time()
                self.stats["completed"] += 1
                self.stats["total_duration_seconds"] += time.time() - start
            future.set_result(result)


# One shared instance for the whole process - every team, every loop
# (dispatch, FMCSA, onboarding validation) submits through this same
# scheduler, which is the entire point (see the module docstring).
#
# max_concurrency=2 (raised from 1, 2026-09-22 - step two of the planned
# rollout, after step one's queue/scheduler refactor was proven behavior-
# neutral at concurrency=1). Verified live before raising this: a real,
# watched, bounded sample (30 companies each) against BOTH Texas's Factor
# ELD tenant and Missouri's Leader ELD tenant, at concurrency=2, gap left
# unchanged at 0.5s to isolate concurrency as the one variable - zero
# 429/403s on either, and a real 30-40% time reduction over concurrency=1
# on the same sample size. Confirmed via the scheduler's own stats that
# real request latency (~0.8-0.9s average) already exceeds the 0.5s gap,
# so concurrency (letting two requests actually overlap in flight) is a
# bigger lever here than further shrinking the gap alone would be.
#
# Next step (not yet done): test concurrency=3 the same way, on its own,
# before raising further - see this file's docstring for why each step is
# tested in isolation rather than jumping straight to a large value.
default_scheduler = EldRequestScheduler(max_concurrency=2, min_gap_seconds=0.5)
