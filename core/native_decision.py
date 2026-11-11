"""Participation transport selection and quiet missing-native-provider fallback."""


def selected_decision_client(host):
    key = 'llm_decision' if getattr(host._runtime_config, 'decision_backend', 'jev') == 'llm' else 'jev'
    return getattr(host, key, None)


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
