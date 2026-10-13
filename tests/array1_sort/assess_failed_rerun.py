#!/usr/bin/env python3
"""Inspect completed AVR reruns and join only exact full-query reference hashes."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import statistics
import time

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[3]
REFERENCE = WORKSPACE / 'c2btor_vs_cpv/avr_array1_repair_20261010T043414.588092Z/crosschecker_reference_20261010T050046.357198Z'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def read_rows(path):
    with path.open(newline='') as stream:
        return list(csv.DictReader(stream))


def controller_start(pid):
    try:
        return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except OSError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--reference', type=Path, default=REFERENCE)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-partial', action='store_true')
    parser.add_argument('--wait-for-completion', action='store_true')
    parser.add_argument('--controller-pid', type=int)
    parser.add_argument('--progress-log', type=Path)
    args = parser.parse_args()
    args.run = args.run.resolve(); args.reference = args.reference.resolve(); args.output = args.output.resolve()
    if args.wait_for_completion:
        if not args.controller_pid or not args.progress_log:
            parser.error('Wait mode requires controller PID and a new progress log')
        initial_start = controller_start(args.controller_pid)
        with args.progress_log.open('x') as stream:
            while True:
                path = args.run / 'results.csv'
                rows = read_rows(path) if path.exists() else []
                value = {'utc': datetime.now(timezone.utc).isoformat(), 'completed': len(rows),
                         'results': dict(Counter(r.get('result') for r in rows)), 'controller_pid': args.controller_pid}
                stream.write(json.dumps(value) + '\n'); stream.flush()
                summary = args.run / 'summary.json'
                if summary.exists():
                    try:
                        json.loads(summary.read_text())
                        break
                    except json.JSONDecodeError:
                        pass
                if controller_start(args.controller_pid) != initial_start or initial_start is None:
                    args.allow_partial = True
                    break
                time.sleep(30)
    args.output.mkdir(parents=True, exist_ok=False)
    selected = json.loads((args.run / 'selected_jobs.json').read_text())
    expected = {f"{int(j['id']):04d}_mixed{int(j['threshold_bits'])}": j for j in selected}
    provenance = json.loads((args.run / 'provenance.json').read_text())
    csv_bytes = (args.run / 'results.csv').read_bytes()
    with (args.output / 'results_snapshot.csv').open('xb') as stream:
        stream.write(csv_bytes)
    rows = read_rows(args.output / 'results_snapshot.csv')
    records, anomalies = {}, []
    for row in rows:
        key = row.get('key')
        if key not in expected or key in records:
            anomalies.append({'key': key, 'reason': 'unexpected-or-duplicate-key'}); continue
        result_path = args.run / 'cells' / key / 'result.json'
        try:
            record = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            anomalies.append({'key': key, 'reason': 'missing-or-partial-result-json'}); continue
        job = expected[key]
        if any(record.get(field) != job[field] or row.get(field) != job[field] for field in ('query_sha256', 'model_sha256', 'map_sha256')):
            anomalies.append({'key': key, 'reason': 'pinned-input-hash-mismatch'}); continue
        if record.get('result') != row.get('result') or record.get('tool_hashes', {}).get('reach_y2bt') != provenance['tools']['reach_y2bt']['sha256']:
            anomalies.append({'key': key, 'reason': 'result-or-backend-hash-mismatch'}); continue
        records[key] = record
    if (len(records) != len(expected) or anomalies) and not args.allow_partial:
        save(args.output / 'incomplete_or_invalid.json', {'expected': len(expected), 'accepted': len(records), 'anomalies': anomalies})
        raise RuntimeError('Full assessment requires every pinned result exactly once')
    pinned = json.loads((args.reference / 'artifact_hashes.json').read_text())
    for name in ('coverage.csv', 'references.csv', 'summary.json', 'conflicts.json'):
        if digest(args.reference / name) != pinned[name]:
            raise RuntimeError('Reference artifact changed: ' + name)
    references = defaultdict(list)
    for row in read_rows(args.reference / 'references.csv'):
        if row['selected_key'] in expected:
            if row['query_sha256'] != expected[row['selected_key']]['query_sha256']:
                raise RuntimeError('Reference does not match selected exact query')
            references[row['selected_key']].append(row)
    comparisons, mismatches, fingerprints = [], [], Counter()
    for key, record in sorted(records.items()):
        refs = references[key]
        raw = {r['raw_result'] for r in refs}
        # A warning-free raw result is still evidence, not independently validated truth.
        warning_free = {r['raw_result'] for r in refs if int(r['constant_array_warning_count']) == 0}
        original_kind_risk = any(r['version'] == 'original' and r['algorithm'] == 'kind' and int(r['constant_array_warning_count']) > 0 for r in refs)
        consensus = next(iter(raw)) if len(raw) == 1 else ('CONFLICT' if raw else 'NO_REFERENCE')
        clean_consensus = next(iter(warning_free)) if len(warning_free) == 1 else ('CONFLICT' if warning_free else 'NO_REFERENCE')
        comparison = {'key': key, 'query_sha256': record['query_sha256'], 'avr_result': record['result'],
                      'raw_reference_consensus': consensus, 'warning_free_raw_consensus': clean_consensus,
                      'reference_runs': len(refs), 'existing_pono_conflict': consensus == 'CONFLICT',
                      'unsupported_kind_proof_risk': original_kind_risk,
                      'raw_agreement': 'match' if record['result'] == consensus else
                                       ('opposite' if record['result'] in ('SAT', 'UNSAT') and consensus in ('SAT', 'UNSAT') else 'not-comparable'),
                      'warning_free_agreement': 'match' if record['result'] == clean_consensus else
                                               ('opposite' if record['result'] in ('SAT', 'UNSAT') and clean_consensus in ('SAT', 'UNSAT') else 'not-comparable'),
                      'avr_log': record.get('log', '')}
        comparisons.append(comparison)
        if comparison['raw_agreement'] == 'opposite' or comparison['warning_free_agreement'] == 'opposite':
            mismatches.append({**comparison, 'references': refs})
        if record['result'] in ('Failed', 'Inconsistent', 'Unsupported'):
            diagnostics = record.get('diagnostics', '')
            messages = {line.strip() for line in diagnostics.splitlines() if re.search(r'Assertion .*failed|AVR backend (?:error|unsupported):|type mismatch|segmentation fault|std::out_of_range', line, re.I)}
            if not messages:
                messages = {diagnostics.splitlines()[0] if diagnostics else record.get('exception', 'no diagnostic')}
            fingerprints.update(messages)
    with (args.output / 'reference_comparison.csv').open('x', newline='') as stream:
        fields = ['key', 'query_sha256', 'avr_result', 'raw_reference_consensus', 'warning_free_raw_consensus', 'reference_runs',
                  'existing_pono_conflict', 'unsupported_kind_proof_risk', 'raw_agreement', 'warning_free_agreement', 'avr_log']
        writer = csv.DictWriter(stream, fields); writer.writeheader(); writer.writerows(comparisons)
    save(args.output / 'discrepancies.json', mismatches)
    save(args.output / 'failure_fingerprints.json', dict(fingerprints))
    final_summary = args.run / 'summary.json'
    run_summary = json.loads(final_summary.read_text()) if final_summary.exists() else None
    times = [float(r['time_s']) for r in records.values() if r.get('time_s') is not None]
    cpus = [float(r['cpu_s']) for r in records.values() if r.get('cpu_s') is not None]
    memories = [float(r['rss_MiB']) for r in records.values() if r.get('rss_MiB') is not None]
    summary = {'utc': datetime.now(timezone.utc).isoformat(), 'expected_cells': len(expected), 'completed_cells': len(records),
               'all_complete': len(records) == len(expected) and not anomalies,
               'results': dict(Counter(r['result'] for r in records.values())),
               'by_threshold': {str(t): dict(Counter(r['result'] for r in records.values() if int(r['threshold_bits']) == t)) for t in (512, 2048)},
               'same_old_assertion_reproduced': sum(r['same_old_assertion_reproduced'] for r in records.values()),
               'backend_failure_seen': sum(r['backend_failure_seen'] for r in records.values()),
               'decisive_raw_answers': sum(r['result'] in ('SAT', 'UNSAT') for r in records.values()),
               'reference_join_counts': dict(Counter(r['raw_agreement'] for r in comparisons)),
               'warning_free_reference_join_counts': dict(Counter(r['warning_free_agreement'] for r in comparisons)),
               'existing_pono_conflicted_cells_completed': sum(r['existing_pono_conflict'] for r in comparisons),
               'opposite_reference_answer_cells': len(mismatches), 'anomalies': anomalies,
               'measured_solver_wall_s_sum': sum(times), 'measured_solver_wall_s_median': statistics.median(times) if times else None,
               'measured_solver_cpu_s_sum': sum(cpus), 'peak_case_rss_MiB': max(memories) if memories else None,
               'final_runner_summary': run_summary, 'final_runner_summary_sha256': digest(final_summary) if run_summary is not None else None,
               'source_program_verdicts': 'Not inferred: all answers are for exact full queries including auxiliary guards.',
               'run': str(args.run), 'run_csv_sha256': hashlib.sha256(csv_bytes).hexdigest(),
               'query_selection_sha256': digest(args.run / 'selected_jobs.json'),
               'reference_artifact_hashes_sha256': digest(args.reference / 'artifact_hashes.json'),
               'script_sha256': digest(Path(__file__)), 'solver_runs_executed': 0}
    save(args.output / 'summary.json', summary)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
