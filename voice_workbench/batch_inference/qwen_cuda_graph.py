"""Experimental, bounded CUDA graph cache. Opt-in; validated on RTX 2080 Ti.

Record the existing fixed loop as-is, including its RNG operations. Warmup,
capture and first-use parity checks must not consume the request's RNG stream.
No torch import or GPU work occurs just by importing/constructing this module.
"""
from collections import OrderedDict


class GraphStateError(RuntimeError):
    """RNG restoration failed: the worker must exit instead of retrying locally."""


class PredictorGraphs:
    # Every entry keeps a private pool alive. Two of them, plus the model, plus a
    # desktop that moves gigabytes in and out of the same 11 GiB card, is how a
    # batch ends up paging; one held shape is enough to keep the fast path fast.
    max_entries = 1
    max_failures = 8
    minimum_free_bytes = 512 * 1024**2
    # Well above minimum_free_bytes: capture is still allowed at 512 MiB, but a
    # batch should not start there. See relief().
    relief_free_bytes = 1536 * 1024**2

    def __init__(self):
        self.entries = OrderedDict()
        self.failed = OrderedDict()
        self.captures = self.replays = self.fallbacks = 0
        self.reliefs = self.lights = 0
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

    def relief(self, device):
        """Return pooled VRAM between batches when the card is close to its limit.

        Measured on one 11 GiB card, same job, same graph, same batch size of 8:
        0.118 s/frame, then four batches at 1.24-1.56 s/frame, then 0.086 s/frame
        again. No capture, no fallback, no model reload in between -- only an
        unrelated batch that happened to capture a new shape, which evicts graph
        pools and empties the allocator first, and the speed came straight back.
        VRAM had drifted to the driver limit (other applications on this machine
        move several GiB in and out), so every frame round-tripped through WDDM
        paging. Leaving that recovery to an unrelated cache miss made it luck, so
        it now runs before each batch.

        Called per batch, never per frame: it costs nothing while memory is
        plentiful, and evicting pools forces a re-capture (about a second) only
        when the alternative is a fifty-second batch.
        """
        import torch
        free_bytes = self._free(device)
        if free_bytes is None or free_bytes >= self.relief_free_bytes:
            return False
        self.reliefs += 1
        empty = getattr(torch.cuda, 'empty_cache', None)
        if empty:
            # Cached blocks are reclaimable without losing a single graph.
            empty()
            free_bytes = self._free(device)
            if free_bytes is None or free_bytes >= self.relief_free_bytes:
                return False
        # Each graph holds a private pool that only dies with the graph itself.
        evicted = False
        while self.entries:
            self.drop(self.entries.popitem(last=False)[1])
            evicted = True
            if empty:
                empty()
            free_bytes = self._free(device)
            if free_bytes is None or free_bytes >= self.relief_free_bytes:
                break
        return evicted

    def light(self, device):
        """Return cached allocator blocks mid-batch, without evicting a graph.

        A batch starts with room and then fills it as it renders: measured here at
        0.090, 0.209, 0.262, 0.265, 0.305, 0.342 s/frame over six batches of four,
        with free VRAM falling from 254 MiB to 120 MiB. Checking only between
        batches misses that entirely.

        Evicting a graph mid-batch would force a re-capture inside the same batch,
        which costs more than the paging it avoids, so this only hands back blocks
        the allocator is not using. That was enough in the one batch that happened
        to do it: 1.29 s/frame down to 0.21.
        """
        free_bytes = self._free(device)
        if free_bytes is None or free_bytes >= self.relief_free_bytes:
            return False
        empty = getattr(__import__('torch').cuda, 'empty_cache', None)
        if empty is None:
            return False
        empty()
        self.lights += 1
        return True

    @staticmethod
    def _free(device):
        import torch
        probe = getattr(torch.cuda, 'mem_get_info', None)
        if probe is None:
            return None
        try:
            return probe(device)[0]
        except (RuntimeError, TypeError):
            return None

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
