#!/usr/bin/env python3
"""Write a new evidence-backed comparison from completed tiny regression runs."""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run', action='append', required=True, help='name=/absolute/result/directory')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
runs = {}
for value in args.run:
    name, path = value.split('=', 1)
    root = Path(path).resolve()
    summary = json.loads((root / 'summary.json').read_text())
    with (root / 'results.csv').open(newline='') as stream:
        records = list(csv.DictReader(stream))
    assert len(records) == summary['cases']
    assert len({r['case'] for r in records}) == len(records)
    runs[name] = {'directory': str(root), 'summary': summary,
                  'results': dict(Counter(r['result'] for r in records)),
                  'records': {r['case']: r for r in records}}
cases = sorted(set.union(*(set(v['records']) for v in runs.values())))
fields = ['case', 'expected'] + [name + '_' + field for name in runs for field in ('result', 'passed', 'witness_status', 'log')]
with (args.output / 'comparison.csv').open('x', newline='') as stream:
    writer = csv.DictWriter(stream, fields)
    writer.writeheader()
    for case in cases:
        expected = {v['records'][case]['expected'] for v in runs.values() if case in v['records']}
        assert len(expected) == 1
        row = {'case': case, 'expected': next(iter(expected))}
        for name, value in runs.items():
            record = value['records'].get(case, {})
            for field in ('result', 'passed', 'witness_status', 'log'):
                row[name + '_' + field] = record.get(field, 'NotRun')
        writer.writerow(row)
with (args.output / 'summary.json').open('x') as stream:
    json.dump({name: {k: v for k, v in value.items() if k != 'records'} for name, value in runs.items()}, stream, indent=2)
print(json.dumps({name: value['results'] for name, value in runs.items()}, indent=2))
