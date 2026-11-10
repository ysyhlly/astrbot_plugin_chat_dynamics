import asyncio
from astrbot.api import logger
from astrbot.api.star import Star,register
from .allocator_guard import AllocatorGuard

@register('astrbot_plugin_memory_guard','local','Return unused allocator pages after memory pressure','1.0.0')
class MemoryGuardPlugin(Star):
    async def initialize(self):
        self.guard=AllocatorGuard();self.task=asyncio.create_task(self._watch(),name='memory_guard_watch')

    async def _watch(self):
        while True:
            # Join the native allocator call before unloading this plugin.
            operation=asyncio.create_task(asyncio.to_thread(self.guard.check))
            try:result=await asyncio.shield(operation)
            except asyncio.CancelledError:
                await operation
                raise
            if result['outcome']=='trimmed':
                logger.info('[MemoryGuard] released_mib=%.1f rss_before_mib=%.1f rss_after_mib=%.1f',
                    result['released']/1048576,result['rss_before']/1048576,result['rss_after']/1048576)
            elif result['outcome']=='error':
                logger.warning('[MemoryGuard] check failed type=%s',result['type'])
            elif result['outcome']=='unsupported':return
            await asyncio.sleep(120)

    async def terminate(self):
        task=getattr(self,'task',None)
        if task is not None:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
