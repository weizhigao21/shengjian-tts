"""Small, session-local execution snapshots; never scan generation history."""
from copy import deepcopy
import time


class InferenceTelemetry:
    def execution_phase(self, slot, phase, inference=None):
        with self.lock:
            if slot in self.active:
                self.active[slot]['phase'] = phase
                if inference is not None:
                    self.active[slot]['inference'] = inference

    def execution_finished(self, slot, status, *, cached=False, inference=None):
        with self.lock:
            if slot in self.active:
                self.last_execution = dict(self.execution_item(self.active[slot]),
                    status=status, cached=cached, finished=time.time())
                if inference is not None:
                    self.last_execution.update(inference)

    @staticmethod
    def execution_item(active):
        indices = active.get('indices', [active['index']])
        return {**active.get('inference', {}), 'job_id': active['job_id'],
                'index': active['index'], 'indices': list(indices),
                'backend': 'qwen3_batch' if active.get('batch') else 'native',
                'batch_size': len(indices), 'phase': active.get('phase', 'checking'),
                'cancelling': active['cancelled'].is_set()}

    def execution_snapshot(self):
        return {'active': [self.execution_item(a) for a in self.active.values()],
                'last_execution': deepcopy(getattr(self, 'last_execution', None))}
