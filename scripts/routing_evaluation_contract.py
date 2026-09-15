"""Offline corpus provenance and independent effectiveness acceptance."""
from astrbot_plugin_chat_dynamics.core.routing_metrics import routing_metrics


def validate_cases(cases):
    """Reject future references and leakage before running any replay."""
    ids, partitions = set(), {}
    for case in cases:
        if case['id'] in ids:
            raise ValueError('duplicate case id')
        ids.add(case['id'])
        session = case.get('session_id', case['id'])
        split = case.get('split', 'unspecified')
        if session in partitions and partitions[session] != split:
            raise ValueError('session crosses dataset splits')
        partitions[session] = split
        previous_at = float('-inf')
        for index, message in enumerate(case['messages']):
            at = message.get('at', index)
            if at < previous_at:
                raise ValueError('replay timestamps must be nondecreasing')
            previous_at = at
            for key in ('reply_to', 'expected_parent'):
                parent = message.get(key)
                if parent is not None and (str(parent) not in {str(i) for i in range(index)}):
                    raise ValueError(f'{key} must reference a visible earlier message')


def effectiveness(cases, results):
    """Synthetic locks and AI-assisted labels cannot establish real-world quality."""
    groups = {}
    for case, result in zip(cases, results):
        source = case.get('label_source', 'unspecified')
        groups.setdefault(source, []).append((case, result))
    by_source = {source: routing_metrics([r['observations'] for _, r in rows])
                 for source, rows in groups.items()}
    eligible = [(c, r) for c, r in groups.get('independent_human', [])
                if c.get('corpus_kind') == 'real' and c.get('split') == 'validation'
                and c.get('session_id') and c.get('reviewer_id')
                and routing_metrics([r['observations']])['topic']['pairs']
                and routing_metrics([r['observations']])['parent']['labeled']]
    metrics = routing_metrics([r['observations'] for _, r in eligible])
    count = len({c['session_id'] for c, _ in eligible})
    reasons = []
    if not metrics['topic']['pairs'] or not metrics['parent']['labeled']:
        reasons.append('insufficient_labels')
    if count < 30:
        reasons.append('insufficient_independent_real_sessions')
    # Zero-error acceptance is deliberately strict; changes require an explicit,
    # reviewed protocol rather than a threshold selected after seeing results.
    quality = (metrics['topic']['wrong_merge'] == 0
               and metrics['topic']['fragmentation'] == 0
               and metrics['parent']['correct'] == metrics['parent']['labeled'])
    return {'status': 'not_ready' if reasons else ('passed' if quality else 'failed'),
            'reasons': reasons, 'independent_validation_sessions': count,
            'required_sessions': 30, 'metrics': metrics, 'by_label_source': by_source,
            'execution': {'status': 'not_measured', 'reason': 'offline replay does not send'},
            'participation': {'status': 'admission_only', 'reason': 'no final model decision'}}
