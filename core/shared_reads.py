"""Share concurrent identical reads without retaining stale results."""
import asyncio
import copy


class SharedReads:
    def __init__(self):
        self.pending = {}
        self.joins = 0

    async def read(self, key, factory):
        entry = self.pending.get(key)
        if entry is None:
            entry = [asyncio.create_task(factory()), 0]
            self.pending[key] = entry
        else:
            self.joins += 1
        task = entry[0]
        entry[1] += 1
        try:
            return copy.deepcopy(await asyncio.shield(task))
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                if self.pending.get(key) is entry:
                    self.pending.pop(key, None)
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
