"""Quiet fallback when no built-in native Jev model has been selected."""


class NativeDecisionUnavailable:
    def configure(self, **kwargs):
        pass

    def snapshot(self):
        return {"configured": False, "available": False, "status": "missing",
                "detail": "native_provider_unavailable", "model": "",
                "calls": 0, "failures": 0, "last_latency_ms": 0}

    async def evaluate(self, *, state, questions, timeout=None):
        return None

    async def close(self):
        pass
