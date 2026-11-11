#!/usr/bin/env python3
"""Score observed semantic routing decisions; never invent predictions from keywords."""
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone


def evaluate(cases, observations):
    evidence = observations.get('evidence_type')
    if evidence not in {'agent_semantic_probe', 'app_router_trace', 'recorded_fixture'}:
        raise ValueError('Explicit supported evidence_type required')
    if not isinstance(observations.get('runner'), str) or not observations['runner'].strip():
        raise ValueError('runner required')
    rows = observations.get('decisions', [])
    expected = {c['id']: c for c in cases}
    if len(expected) != len(cases):
        raise ValueError('Duplicate case id')
    seen = {}
    for row in rows:
        if (row['id'] in seen or row['id'] not in expected
                or not isinstance(row.get('reason'), str) or not row['reason'].strip()):
            raise ValueError('Duplicate, unknown or unexplained decision')
        seen[row['id']] = row
    results = []
    for cid, case in expected.items():
        got = seen.get(cid, {})
        passed = got.get('skill') == case['expected_skill'] and got.get('stage') == case['expected_stage']
        results.append({'id': cid, 'passed': passed, 'expected_skill': case['expected_skill'],
                        'expected_stage': case['expected_stage'], 'observed': got or None})
    passed = sum(r['passed'] for r in results)
    return {'ok': bool(results) and passed == len(results), 'evidence_type': evidence,
            'runner': observations['runner'], 'model': observations.get('model', 'unavailable'),
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'summary': {'total': len(results), 'passed': passed}, 'results': results,
            'limitation': 'Semantic probe is not evidence of actual application skill routing; fixtures are not model runs.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases', required=True)
    p.add_argument('--observed', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    cases = json.loads(Path(a.cases).read_text())['cases']
    observed = json.loads(Path(a.observed).read_text())
    result = evaluate(cases, observed)
    out = Path(a.output); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('ok','evidence_type','summary')}))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
