#!/usr/bin/env python3
"""Freeze completed strict AVR mixed512/2048 backend failures, not UNKNOWNs."""
import collections
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[3]
SOURCE = WORKSPACE / 'c2btor_vs_cpv/mixed_full_avr_fixed_1core15g_20261009T1830Z'
RECORDS = WORKSPACE / 'c2btor_vs_cpv/avr_array1_repair_20261010T043414.588092Z'
FAIL = re.compile(r'Assertion .*failed|\bf_err\b|type mismatch|type error|types? (?:do not|does not) match|segmentation fault|Aborted', re.I)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write('\n')


def array1_count(model):
    sorts = {}
    with model.open() as stream:
        for line in stream:
            fields = line.split(';', 1)[0].split()
            if len(fields) >= 4 and fields[1] == 'sort':
                sorts[int(fields[0])] = fields[2:]
    return sum(sort[0] == 'array' and sorts[int(sort[2])] == ['bitvec', '1'] for sort in sorts.values())


def main():
    output = RECORDS / ('failed_subset_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
    output.mkdir(); (output / 'evidence').mkdir()
    # Snapshot the bytes once; the historical controller may keep appending.
    snapshot = (SOURCE / 'results.csv').read_bytes()
    with (output / 'historical_results_snapshot.csv').open('xb') as stream:
        stream.write(snapshot)
    rows = list(csv.DictReader(io.StringIO(snapshot.decode())))
    selected, incomplete, excluded, fingerprints = [], [], collections.Counter(), collections.Counter()
    for row in rows:
        if row.get('encoding') != 'mixed' or row.get('threshold_bits') not in ('512', '2048'):
            continue
        directory = Path(row.get('run_directory') or '')
        result_file = directory / 'result.json'
        resource_file = directory / 'avr/full.resources.json'
        log = directory / 'avr/full.log'
        try:
            completed = json.loads(result_file.read_text()); resource = json.loads(resource_file.read_text())
            if completed['id'] != int(row['id']) or str(completed['threshold_bits']) != row['threshold_bits']:
                raise ValueError('Completed record identity mismatch')
            if any(str(completed.get(k, '')) != str(row.get(k, '')) for k in ('full_result', 'full_exit_code', 'model_sha256', 'map_sha256')):
                raise ValueError('Completed record differs from CSV snapshot')
        except (OSError, ValueError, KeyError, TypeError) as error:
            incomplete.append({'id': row.get('id'), 'threshold_bits': row.get('threshold_bits'), 'reason': str(error)})
            continue
        if resource['status'] in ('Timeout', 'OOM') or row['full_result'] in ('Timeout', 'OOM'):
            excluded['resource-limit'] += 1; continue
        text = log.read_text(errors='replace')
        errors = [line.strip() for line in text.splitlines() if FAIL.search(line)]
        if not errors:
            excluded['no-crash-or-type-error'] += 1; continue
        # Runtime error is required. Source/guard UNKNOWN without such a log
        # is never pulled into this repair validation subset.
        if row['full_result'] in ('SAT', 'UNSAT'):
            excluded['conclusive-full-query-with-diagnostic'] += 1; continue
        model = Path(row['model_path']); mapping = Path(row['map_path'])
        if digest(model) != row['model_sha256'] or digest(mapping) != row['map_sha256']:
            raise RuntimeError('Historical generation model/map hash changed')
        query = directory / 'proof_full.btor2'
        array1 = array1_count(query)
        key = f"{int(row['id']):04d}_mixed{row['threshold_bits']}"
        evidence = output / 'evidence' / key; evidence.mkdir()
        with (evidence / 'historical_full.log').open('xb') as stream:
            stream.write(log.read_bytes())
        save(evidence / 'historical_row_and_resources.json', {'row': row, 'resources': resource, 'completed_result': completed,
             'original_log': str(log), 'original_log_sha256': digest(log), 'diagnostics': errors})
        entry = {'file': str(query), 'label': 'UNKNOWN', 'case_id': int(row['id']), 'id': int(row['id']),
                 'threshold_bits': int(row['threshold_bits']), 'source_file': row['file'],
                 'source_label': 'SAFE' if row['label'].lower() in ('true', 'safe') else 'UNSAFE',
                 'expected_full_query_result': '', 'old_result': row['full_result'],
                 'old_exit_code': row['full_exit_code'], 'old_backend_failure': 'f_err/assert/type-error',
                 'old_log': str(log), 'old_log_sha256': digest(log), 'query_sha256': digest(query),
                 'model_path': str(model), 'model_sha256': row['model_sha256'],
                 'map_path': str(mapping), 'map_sha256': row['map_sha256'],
                 'array1_sort_count': array1, 'query_bytes': query.stat().st_size,
                 'diagnostics': '\n'.join(errors), 'evidence': str(evidence)}
        selected.append(entry); fingerprints[errors[0]] += 1
    selected.sort(key=lambda row: (row['id'], row['threshold_bits']))
    if len({(row['id'], row['threshold_bits']) for row in selected}) != len(selected):
        raise RuntimeError('Duplicate failure cells')
    with (output / 'manifest.csv').open('x', newline='') as stream:
        writer = csv.DictWriter(stream, list(selected[0]) if selected else ['file', 'label', 'case_id'])
        writer.writeheader(); writer.writerows(selected)
    save(output / 'jobs.json', selected)
    summary = {'snapshot_utc': datetime.now(timezone.utc).isoformat(), 'source': str(SOURCE),
               'source_rows_at_snapshot': len(rows), 'snapshot_sha256': hashlib.sha256(snapshot).hexdigest(),
               'selected_failure_cells': len(selected), 'unique_sources': len({r['id'] for r in selected}),
               'threshold_counts': dict(collections.Counter(r['threshold_bits'] for r in selected)),
               'all_selected_have_array1': all(r['array1_sort_count'] for r in selected),
               'fingerprints': dict(fingerprints), 'incomplete_records_ignored': incomplete,
               'excluded': dict(excluded), 'solver_runs_executed': 0,
               'query_semantics': 'original full-proof query includes source and model-limit/memory-validity obligations; raw SAT is not a source UNSAFE verdict',
               'label_policy': 'manifest label UNKNOWN; separate source_label is context, not an expected answer for the full query',
               'manifest': str(output / 'manifest.csv'), 'jobs_sha256': digest(output / 'jobs.json')}
    save(output / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
