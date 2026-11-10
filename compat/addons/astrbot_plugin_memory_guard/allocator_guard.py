"""Return glibc's already-free pages after a sustained RSS rise."""
from dataclasses import dataclass
import ctypes
from pathlib import Path
import sys

MIB=1024*1024

class MallInfo(ctypes.Structure):
    _fields_=[(name,ctypes.c_size_t) for name in ('arena','ordblks','smblks','hblks','hblkhd','usmblks','fsmblks','uordblks','fordblks','keepcost')]

@dataclass(frozen=True)
class MemoryState:
    rss:int
    free_heap:int
    used_heap:int

class AllocatorGuard:
    def __init__(self,reader=None,trim=None,*,minimum_rss=1024*MIB,minimum_free=256*MIB,growth=128*MIB):
        self.reader=reader;self.trim=trim
        self.minimum_rss=minimum_rss;self.minimum_free=minimum_free;self.growth=growth
        self.watermark=None;self.last={};self.libc=None
        if reader is not None and trim is not None:return
        if sys.platform!='linux':return
        try:
            libc=ctypes.CDLL(None)
            libc.gnu_get_libc_version.restype=ctypes.c_char_p
            libc.gnu_get_libc_version()
            libc.mallinfo2.restype=MallInfo
            libc.malloc_trim.argtypes=[ctypes.c_size_t];libc.malloc_trim.restype=ctypes.c_int
            self.libc=libc;self.reader=self._read;self.trim=lambda:libc.malloc_trim(0)
        except (AttributeError,OSError):pass

    def _read(self):
        status=Path('/proc/self/status').read_text()
        rss=next(int(line.split()[1])*1024 for line in status.splitlines() if line.startswith('VmRSS:'))
        info=self.libc.mallinfo2()
        return MemoryState(rss=rss,free_heap=info.fordblks,used_heap=info.uordblks)

    def check(self):
        if self.reader is None or self.trim is None:
            self.last={'outcome':'unsupported'};return self.last
        try:
            before=self.reader()
            if (before.rss<self.minimum_rss or before.free_heap<self.minimum_free):
                self.watermark=before.rss
                self.last={'outcome':'no_pressure','rss':before.rss,'free_heap':before.free_heap};return self.last
            if self.watermark is not None and before.rss<self.watermark+self.growth:
                # The allocator's virtual free size stays high even after trim;
                # avoid expensive repeated walks without a new resident rise.
                self.last={'outcome':'stable','rss':before.rss,'free_heap':before.free_heap};return self.last
            self.trim()
            after=self.reader();self.watermark=after.rss
            self.last={'outcome':'trimmed','rss_before':before.rss,'rss_after':after.rss,
                       'released':max(0,before.rss-after.rss),'free_heap_before':before.free_heap,
                       'used_heap_before':before.used_heap,'used_heap_after':after.used_heap}
            return self.last
        except Exception as exc:
            self.last={'outcome':'error','type':type(exc).__name__};return self.last
