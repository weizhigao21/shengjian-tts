"""Experimental, bounded CUDA graph cache. Opt-in; validated on RTX 2080 Ti.

Record the existing fixed loop as-is, including its RNG operations. Warmup,
capture and first-use parity checks must not consume the request's RNG stream.
No torch import or GPU work occurs just by importing/constructing this module.
"""
from collections import OrderedDict


class GraphStateError(RuntimeError):
    """RNG restoration failed: the worker must exit instead of retrying locally."""


class PredictorGraphs:
    max_entries = 2
    max_failures = 8
    minimum_free_bytes = 512 * 1024**2

    def __init__(self):
        self.entries = OrderedDict()
        self.failed = OrderedDict()
        self.captures = self.replays = self.fallbacks = 0
        self.reason = ''

    @staticmethod
    def key(embeds, parameters):
        return (tuple(embeds.shape), tuple(embeds.stride()), str(embeds.dtype),
                str(embeds.device), *parameters)

    def reject(self, key, reason):
        self.reason = str(reason)[:500]
        self.failed[key] = self.reason
        while len(self.failed) > self.max_failures:
            self.failed.popitem(last=False)

    def capture(self, embeds, forward):
        import torch
        rng = torch.cuda.get_rng_state(embeds.device)
        static = embeds.clone()
        try:
            stream = torch.cuda.Stream(device=embeds.device)
            stream.wait_stream(torch.cuda.current_stream(embeds.device))
            with torch.cuda.stream(stream):
                for _ in range(3):
                    forward(static)
            torch.cuda.current_stream(embeds.device).wait_stream(stream)
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph, stream=stream):
                output = forward(static)
            torch.cuda.synchronize(embeds.device)
            # Validate both tokens and the following RNG position. This is outside
            # capture: CPU/GPU synchronization is forbidden during recording.
            torch.cuda.set_rng_state(rng, embeds.device)
            expected = forward(static).clone()
            expected_rng = torch.cuda.get_rng_state(embeds.device)
            torch.cuda.set_rng_state(rng, embeds.device)
            graph.replay()
            if not torch.equal(expected, output) or not torch.equal(expected_rng, torch.cuda.get_rng_state(embeds.device)):
                raise RuntimeError('图重放的编码或随机数状态不一致，保留专用循环。')
            return graph, static, output
        finally:
            try:
                torch.cuda.set_rng_state(rng, embeds.device)
            except RuntimeError as exc:
                raise GraphStateError('图录制后无法恢复随机数状态，释放工作进程并回退。') from exc

    def drop(self, entry):
        import torch
        torch.cuda.synchronize(entry[1].device)
        del entry

    def make_room(self, embeds):
        """Release cached graph pools before the free-memory gate.

        An earlier graph keeps its private pool reserved in PyTorch's allocator, so
        the driver reports almost no free memory and a new batch shape (for example
        a shorter trailing group) would be refused even though those blocks are
        reclaimable. Evict, return the blocks, then re-check.
        """
        import torch
        while len(self.entries) >= self.max_entries:
            self.drop(self.entries.popitem(last=False)[1])
        empty = getattr(torch.cuda, 'empty_cache', None)
        if empty:
            empty()
        if self.entries and torch.cuda.mem_get_info(embeds.device)[0] < self.minimum_free_bytes:
            # Only the current shape is worth keeping when memory is this tight.
            self.drop(self.entries.popitem(last=False)[1])
            if empty:
                empty()

    def run(self, embeds, parameters, forward):
        import torch
        if (embeds.device.type != 'cuda' or embeds.dtype != torch.float16
                or not 1 <= embeds.shape[0] <= 8
                or torch.__version__.split('+')[0] != '2.9.0'):
            self.reason = '实验图加速仅适配 PyTorch 2.9.0 / CUDA / FP16 / 1–8 条。'
            self.fallbacks += 1
            return forward(embeds)
        key = self.key(embeds, parameters)
        if key in self.failed:
            self.reason = self.failed[key]
            self.fallbacks += 1
            return forward(embeds)
        if key not in self.entries:
            self.make_room(embeds)
            if torch.cuda.mem_get_info(embeds.device)[0] < self.minimum_free_bytes:
                self.reject(key, '空闲显存不足 512 MiB，保留专用循环。')
                self.fallbacks += 1
                return forward(embeds)
            try:
                self.entries[key] = self.capture(embeds, forward)
                self.captures += 1
            except GraphStateError:
                raise
            except RuntimeError as exc:
                self.reject(key, '图录制失败，保留专用循环：' + str(exc))
                # A poisoned CUDA context must go through the existing worker /
                # native fallback, never retry with uncertain GPU/RNG state.
                torch.cuda.synchronize(embeds.device)
                self.fallbacks += 1
                return forward(embeds)
        graph, static, output = self.entries.pop(key)
        self.entries[key] = graph, static, output
        static.copy_(embeds)
        graph.replay()
        self.replays += 1
        self.reason = ''
        # The next replay reuses the output buffer; return independent storage.
        return output.clone()
