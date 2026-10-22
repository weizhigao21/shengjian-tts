"""Shrink a stalled batch instead of leaving the batch engine.

The stall guard watches frames, not the clock: a batch that stops producing a
frame for 90 s is either wedged or starved of GPU time, and killing it costs a
model reload. Handing that whole cohort to the native engine costs more -- it
renders one row at a time, which is slower than the wait it was meant to avoid
(0.7.65/0.7.66 spent two releases undoing exactly that).

So a stall keeps the rows on the batch engine and trims the micro-batch by two
rows (8 -> 6 -> 4). Below four rows a micro-batch is no longer cheaper than the
native engine, so that is the floor: one more attempt at the floor, then the
native engine takes over. Three consecutive finished batches raise the size back
by two, so a machine that was only briefly busy returns to the configured size
without a restart.
"""

RETRYABLE = {'INFERENCE_TIMEOUT', 'BATCH_BACKEND_FAILED', 'GPU_OUT_OF_MEMORY'}
# Two rows per step: the measured gap between "the card has room" (0.09-0.15 s
# per frame) and "another program took the VRAM" (1.2-1.5 s per frame) needs a
# visibly smaller batch, while halving 8 straight to 4 throws away half the
# throughput when six rows would have fit.
STEP = 2
FLOOR = 4
RECOVER_AFTER = 3


class AdaptiveBatches:
    def __init__(self, settings):
        self.reset(settings)

    def reset(self, settings):
        self.maximum = max(2, settings.max_batch_size)
        self.floor = min(FLOOR, self.maximum)
        self.cap = self.maximum
        self.successes = 0
        self.strikes = {}
        self.reason = ''

    def limit(self, settings=None):
        """The micro-batch size to select with; never above the configured size."""
        if settings is not None and max(2, settings.max_batch_size) != self.maximum:
            # The configured size changed outside configure(): start from it again.
            self.reset(settings)
        return self.cap

    def degrade(self, job_id, error):
        """Trim the batch after a retryable failure, or return '' to give up.

        Returning a reason means "requeue these rows": the caller restarts the
        worker (the stalled one cannot be trusted) and the next selection picks
        the smaller batch. Anything else is left to the existing fallback.
        """
        if error.get('code') not in RETRYABLE:
            return ''
        if self.cap > self.floor:
            self.cap = max(self.floor, self.cap - STEP)
            self.successes = 0
            self.strikes.pop(job_id, None)
            self.reason = f"{error['message']}；已把每批降到 {self.cap} 条重试。"
            return self.reason
        if self.cap == self.maximum:
            # Nothing to trim: a smaller batch is not an option at this size, so a
            # retry would only repeat the same failure and delay the native engine.
            return ''
        if self.strikes.get(job_id, 0) >= 1:
            return ''
        self.strikes[job_id] = 1
        self.reason = f"{error['message']}；已按每批 {self.cap} 条再试一次，仍失败则改用原生引擎。"
        return self.reason

    def succeeded(self, job_id):
        """Count a finished batch and step the size back up when it is steady."""
        self.strikes.pop(job_id, None)
        self.successes += 1
        if self.cap < self.maximum and self.successes >= RECOVER_AFTER:
            self.cap = min(self.maximum, self.cap + STEP)
            self.successes = 0
            self.reason = f"已连续完成 {RECOVER_AFTER} 批，每批大小恢复到 {self.cap} 条。"
            return self.reason
        return ''

    def status(self):
        return {'batch_limit': self.cap, 'batch_limit_max': self.maximum,
                'degraded': self.cap < self.maximum, 'reason': self.reason}
