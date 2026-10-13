#!/usr/bin/env python3
"""Rerun only the pinned, completed historical AVR failures; never regenerate queries."""
import argparse
from collections import Counter, deque
import concurrent.futures
import csv
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sys
import threading

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[3]
SUBSET = WORKSPACE / 'c2btor_vs_cpv/avr_array1_repair_20261010T043414.588092Z/failed_subset_20261010T044045.991371Z/jobs.json'
SUBSET_SHA256 = '3edec0d81cc03dedc80752b296d6463404987e3990746fd6bde35d83c897d893'
FROZEN = WORKSPACE / 'c2btor_vs_cpv/pono_static_mixed_matrix_20261010T040959.693061Z/frozen'
HELPER = FROZEN / 'cbmc/regression/goto-btor2/evaluate_mixed_array.py'
FAIL = re.compile(r'Assertion .*failed|\bf_err\b|type mismatch|type error|types? (?:do not|does not) match|segmentation fault|Aborted|terminate called after|std::out_of_range', re.I)
OLD_ASSERT = re.compile(r'add_gate_constraint\([^\n]*Assertion .*failed', re.I)
OOM = re.compile(r'std::bad_alloc|cannot allocate memory|out of memory|memory allocation .*failed|\bf_mo\b', re.I)


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def key(job):
    return f"{int(job['id']):04d}_mixed{int(job['threshold_bits'])}"


def cpu_set(value):
    result = []
    for item in value.split(','):
        endpoints = item.split('-')
        if len(endpoints) == 1:
            result.append(int(endpoints[0]))
        elif len(endpoints) == 2:
            lo, hi = map(int, endpoints)
            if lo > hi:
                raise ValueError('reversed CPU interval')
            result.extend(range(lo, hi + 1))
        else:
            raise ValueError('invalid CPU interval')
    if not result or len(result) != len(set(result)):
        raise ValueError('CPU list must be nonempty and distinct')
    return result


def import_limits(path):
    spec = importlib.util.spec_from_file_location('avr_rerun_limits', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evidence(F, directory, resource):
    paths = [Path(resource['log'])]
    for path in directory.rglob('*'):
        if path.is_file() and (path.name in ('reach.output', 'vwn.output', 'dpa.output', 'avr.err') or path.suffix == '.results'):
            paths.append(path)
    diagnostics, first, verdicts, entries = deque(maxlen=256), {}, set(), []
    for path in sorted(set(paths)):
        # Scan every byte without holding multi-gigabyte AVR dumps in the
        # controller. Keep complete relevant short messages for classification.
        with path.open(errors='replace') as stream:
            for line in iter(lambda: stream.readline(65536), ''):
                if FAIL.search(line) or OOM.search(line) or re.search(r'Verification result:|\bf_to\b|\bunknown\b|AVR backend unsupported:', line, re.I):
                    diagnostics.append(line[:4096])
                    for name, matched in (('failure', FAIL.search(line)), ('old_assert', OLD_ASSERT.search(line)),
                                          ('oom', OOM.search(line)), ('timeout', re.search(r'\bf_to\b', line)),
                                          ('unsupported', 'AVR backend unsupported:' in line),
                                          ('unknown', re.search(r'\bunknown\b', line, re.I))):
                        if matched and name not in first:
                            first[name] = line[:4096]
                    verdicts.update(re.findall(r'Verification result:\s*avr-([hv])\b', line))
        entries.append({'path': str(path), 'sha256': F.digest(path), 'bytes': path.stat().st_size})
    # Preserve both conflicting verdicts even when later diagnostic text fills
    # the bounded buffer; the underlying complete logs remain untouched.
    text = '\n'.join(list(first.values()) + list(diagnostics) + ['Verification result: avr-' + v for v in sorted(verdicts)])
    return text, entries


def classify(resource, text):
    if resource['status'] in ('Timeout', 'OOM'):
        return resource['status']
    if OOM.search(text):
        return 'OOM'
    if re.search(r'\bf_to\b', text):
        return 'Timeout'
    if resource['status'] != 'Completed':
        return 'Failed'
    if 'AVR backend unsupported:' in text:
        return 'Unsupported'
    answers = re.findall(r'Verification result:\s*avr-([hv])\b', text)
    if len(set(answers)) > 1:
        return 'Inconsistent'
    if FAIL.search(text):
        return 'Failed'
    if answers:
        return {'h': 'UNSAT', 'v': 'SAT'}[answers[-1]]
    return 'UNKNOWN' if re.search(r'\bunknown\b', text, re.I) else 'Failed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--subset', type=Path, default=SUBSET)
    parser.add_argument('--subset-sha256', default=SUBSET_SHA256)
    parser.add_argument('--avr-root', type=Path, default=HERE.parents[1])
    parser.add_argument('--avr-bin', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=8)
    parser.add_argument('--cpu-set', default='96-103')
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--pilot-smallest', type=int, choices=(10, 16))
    parser.add_argument('--witness', action='store_true', help='Generate and separately check SAT witnesses; off preserves historical flags.')
    parser.add_argument('--reuse-from', type=Path, action='append', default=[])
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    F = import_limits(HELPER)
    try:
        cpus = cpu_set(args.cpu_set)
    except ValueError as error:
        parser.error(str(error))
    if args.jobs < 1 or args.jobs > len(cpus) or not set(cpus).issubset(os.sched_getaffinity(0)):
        parser.error('Each active job requires one distinct available CPU')
    if not 0 < args.timeout <= 300:
        parser.error('Timeout must be >0 and <=300 seconds')
    args.subset = args.subset.resolve(); args.avr_root = args.avr_root.resolve(); args.avr_bin = args.avr_bin.resolve()
    if F.digest(args.subset) != args.subset_sha256:
        raise RuntimeError('Pinned failure subset hash mismatch')
    jobs = json.loads(args.subset.read_text())
    if len(jobs) != 881 or len({key(j) for j in jobs}) != 881:
        raise RuntimeError('Pinned failure matrix must have 881 distinct cells')
    if any(int(j['threshold_bits']) not in (512, 2048) or j['label'] != 'UNKNOWN' for j in jobs):
        raise RuntimeError('Unexpected threshold or raw-full-query label')
    if args.pilot_smallest:
        ordinary = sorted((j for j in jobs if 'Segmentation fault' not in j['diagnostics']), key=lambda j: (j['query_bytes'], key(j)))[:args.pilot_smallest]
        frontend = [j for j in jobs if 'Segmentation fault' in j['diagnostics']]
        jobs = ordinary + frontend
    args.output = args.output.resolve(); args.output.mkdir(parents=True, exist_ok=False)
    frozen = args.output / 'frozen'; binary_dir = frozen / 'bin'; binary_dir.mkdir(parents=True)
    inputs = frozen / 'queries'; inputs.mkdir()
    tool_sources = {'avr_py': args.avr_root / 'avr.py', 'avr_shell': args.avr_root / 'avr',
                    'vwn': args.avr_bin / 'vwn', 'dpa': args.avr_bin / 'dpa', 'reach_y2bt': args.avr_bin / 'reach_y2bt',
                    'limits': HELPER, 'contract': FROZEN / 'cbmc/scripts/c2btor_model_contract.py'}
    if args.witness:
        tool_sources['btorsim'] = FROZEN / 'bin/btorsim'
    tool_hashes = {name: F.digest(path) for name, path in tool_sources.items()}
    for name in ('vwn', 'dpa', 'reach_y2bt'):
        shutil.copy2(tool_sources[name], binary_dir / name)
        if F.digest(binary_dir / name) != tool_hashes[name]:
            raise RuntimeError('Binary changed during snapshot: ' + name)
    for name, relative in (('limits', 'cbmc/regression/goto-btor2/evaluate_mixed_array.py'), ('contract', 'cbmc/scripts/c2btor_model_contract.py')):
        dest = frozen / relative; dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tool_sources[name], dest)
        if F.digest(dest) != tool_hashes[name]:
            raise RuntimeError('Resource dependency changed during snapshot: ' + name)
    F = import_limits(frozen / 'cbmc/regression/goto-btor2/evaluate_mixed_array.py')
    for name in ('avr_py', 'avr_shell'):
        shutil.copy2(tool_sources[name], frozen / tool_sources[name].name)
    if args.witness:
        shutil.copy2(tool_sources['btorsim'], binary_dir / 'btorsim')
    query_paths, checked = {}, set()
    for job in jobs:
        for path_field, hash_field in (('file', 'query_sha256'), ('model_path', 'model_sha256'), ('map_path', 'map_sha256'), ('old_log', 'old_log_sha256')):
            path, digest = Path(job[path_field]), job[hash_field]
            if (str(path), digest) not in checked:
                if F.digest(path) != digest:
                    raise RuntimeError('Pinned evidence changed: ' + str(path))
                checked.add((str(path), digest))
        query = inputs / (job['query_sha256'] + '.btor2')
        if not query.exists():
            shutil.copy2(job['file'], query)
            if F.digest(query) != job['query_sha256']:
                raise RuntimeError('Query copy hash mismatch')
        query_paths[key(job)] = query
    provenance = {'utc': datetime.now(timezone.utc).isoformat(), 'argv': sys.argv,
                  'script_sha256': F.digest(Path(__file__)), 'subset_sha256': args.subset_sha256,
                  'cells': len(jobs), 'full_subset_cells': 881, 'jobs': args.jobs, 'cpu_set': cpus,
                  'cores_per_cell': 1, 'memory_gib_per_cell': 15, 'timeout_s': args.timeout, 'file_limit_gib': 1,
                  'backend': 'y2bt', 'avr_cwd': str(args.avr_root), 'witness_enabled': args.witness,
                  'tools': {n: {'path': str(p), 'sha256': tool_hashes[n]} for n, p in tool_sources.items()},
                  'query_semantics': 'Exact historical full proof query; SAT may be a guard. No source-label match computed.',
                  'dry_run': args.dry_run, 'reuse_from': [str(p.resolve()) for p in args.reuse_from]}
    save(args.output / 'provenance.json', provenance); save(args.output / 'selected_jobs.json', jobs)
    if args.dry_run:
        save(args.output / 'dry_run.json', {'validated_cells': len(jobs), 'unique_query_hashes': len({j['query_sha256'] for j in jobs}), 'solver_runs': 0,
              'selection': [key(j) for j in jobs], 'tools_sha256': tool_hashes})
        print(json.dumps({'output': str(args.output), 'selected_cells': len(jobs), 'solver_runs': 0}), flush=True)
        return
    cpu_queue = queue.Queue()
    for cpu in cpus:
        cpu_queue.put(cpu)
    cells = args.output / 'cells'; cells.mkdir()
    lifecycle = args.output / 'lifecycle.jsonl'
    lifecycle_lock = threading.Lock()
    lifecycle_stream = lifecycle.open('x')

    def event(value):
        with lifecycle_lock:
            lifecycle_stream.write(json.dumps({'utc': datetime.now(timezone.utc).isoformat(), **value}) + '\n')
            lifecycle_stream.flush()

    def reusable(job):
        for prior in args.reuse_from:
            path = prior.resolve() / 'cells' / key(job) / 'result.json'
            if not path.exists():
                continue
            try:
                record = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                event({'event': 'invalid_prior_result', 'path': str(path)})
                continue
            if not isinstance(record, dict):
                event({'event': 'invalid_prior_result', 'path': str(path)})
                continue
            if (record.get('key') == key(job) and record.get('query_sha256') == job['query_sha256'] and
                record.get('model_sha256') == job['model_sha256'] and record.get('map_sha256') == job['map_sha256'] and
                record.get('tool_hashes') == tool_hashes and record.get('witness_enabled') == args.witness and
                record.get('timeout_limit_s') == args.timeout and record.get('resource_status') is not None):
                return {**record, 'reused_from': str(path)}
        return None

    def run_one(job):
        directory = cells / key(job); directory.mkdir()
        record = reusable(job)
        if record is not None:
            save(directory / 'result.json', record)
            event({'event': 'reused', 'key': key(job), 'path': record['reused_from']})
            return record
        cpu = cpu_queue.get()
        event({'event': 'started', 'key': key(job), 'cpu': cpu, 'directory': str(directory)})
        base = {**job, 'key': key(job), 'cpu': cpu, 'tool_hashes': tool_hashes,
                'witness_enabled': args.witness, 'timeout_limit_s': args.timeout, 'reused_from': '',
                'old_failure_kind': 'add_gate_constraint_assertion' if OLD_ASSERT.search(job['diagnostics']) else
                                    ('frontend_segmentation_fault' if 'Segmentation fault' in job['diagnostics'] else 'other_backend_failure')}
        try:
            for name in ('avr_py', 'avr_shell'):
                if F.digest(tool_sources[name]) != tool_hashes[name]:
                    raise RuntimeError('Pinned AVR driver changed: ' + name)
            query = query_paths[key(job)]
            if F.digest(query) != job['query_sha256']:
                raise RuntimeError('Frozen query changed')
            command = [sys.executable, tool_sources['avr_py'], '--bt', query, '-o', directory / 'avr_output', '-n', key(job),
                       '--bin', binary_dir, '--backend', 'y2bt', '--timeout', str(max(1, int(args.timeout) - 1)), '--memout', '15360']
            if args.witness:
                command.append('--witness')
            resource = F.run_limited(command, directory, 'full', args.timeout, 15, [cpu], cwd=args.avr_root, fsize_gib=1)
            text, sources = evidence(F, directory, resource)
            result = classify(resource, text)
            diagnostics = '\n'.join(line for line in text.splitlines() if FAIL.search(line) or OOM.search(line) or 'AVR backend unsupported:' in line)[-16384:]
            record = {**base, 'result': result, 'resource_status': resource['status'], 'exit_code': resource['exit_code'],
                      'time_s': resource['time_s'], 'cpu_s': resource['cpu_s'], 'rss_MiB': resource['rss_MiB'],
                      'same_old_assertion_reproduced': bool(OLD_ASSERT.search(job['diagnostics']) and OLD_ASSERT.search(text)),
                      'backend_failure_seen': bool(FAIL.search(text)), 'diagnostics': diagnostics,
                      'log': resource['log'], 'log_sources': sources, 'witness_status': 'NotRequested'}
            if args.witness and result == 'SAT':
                witnesses = list(directory.rglob('cex.witness'))
                record['witness_status'] = 'MissingOrAmbiguous'
                if len(witnesses) == 1 and witnesses[0].stat().st_size:
                    replay = F.run_limited([binary_dir / 'btorsim', '-c', query, witnesses[0]], directory, 'witness_replay', 10, 15, [cpu], cwd=WORKSPACE, fsize_gib=1)
                    record.update(witness_status='Validated' if replay['status'] == 'Completed' and replay['exit_code'] == 0 else 'InvalidOrUnsupported',
                                  witness=str(witnesses[0]), witness_sha256=F.digest(witnesses[0]), witness_log=replay['log'])
        except Exception as error:
            record = {**base, 'result': 'Failed', 'resource_status': 'HarnessException', 'exception': repr(error),
                      'same_old_assertion_reproduced': False, 'backend_failure_seen': False,
                      'diagnostics': repr(error), 'witness_status': 'NotRun'}
        finally:
            cpu_queue.put(cpu)
        save(directory / 'result.json', record)
        event({'event': 'finished', 'key': key(job), 'cpu': cpu, 'result': record['result']})
        return record

    fields = ['key', 'id', 'threshold_bits', 'result', 'source_label', 'source_file', 'old_result', 'old_backend_failure', 'old_failure_kind',
              'same_old_assertion_reproduced', 'backend_failure_seen', 'resource_status', 'exit_code', 'time_s', 'cpu_s', 'rss_MiB',
              'cpu', 'query_sha256', 'model_sha256', 'map_sha256', 'diagnostics', 'witness_status', 'log', 'reused_from']
    results = []
    with (args.output / 'results.csv').open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fields, extrasaction='ignore'); writer.writeheader(); stream.flush()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(run_one, job): job for job in jobs}
            for future in concurrent.futures.as_completed(futures):
                record = future.result(); results.append(record)
                writer.writerow(record); stream.flush()
                print(json.dumps({'completed': len(results), 'total': len(jobs), 'key': record['key'], 'result': record['result'],
                                  'backend_failure_seen': record['backend_failure_seen'], 'time_s': record.get('time_s')}), flush=True)
    lifecycle_stream.close()
    changed = {name: {'expected': tool_hashes[name], 'actual': F.digest(path)} for name, path in tool_sources.items() if F.digest(path) != tool_hashes[name]}
    save(args.output / 'summary.json', {'cells': len(results), 'results': dict(Counter(r['result'] for r in results)),
         'by_threshold': {str(t): dict(Counter(r['result'] for r in results if int(r['threshold_bits']) == t)) for t in (512, 2048)},
         'by_old_failure': {kind: dict(Counter(r['result'] for r in results if r['old_backend_failure'] == kind)) for kind in sorted({r['old_backend_failure'] for r in results})},
         'by_old_failure_kind': {kind: dict(Counter(r['result'] for r in results if r['old_failure_kind'] == kind)) for kind in sorted({r['old_failure_kind'] for r in results})},
         'same_old_assertion_reproduced': sum(r['same_old_assertion_reproduced'] for r in results),
         'backend_failure_seen': sum(r['backend_failure_seen'] for r in results),
         'reused': sum(bool(r['reused_from']) for r in results), 'changed_tool_files': changed,
         'source_program_verdicts': 'Not inferred: this rerun checks exact full-proof queries including auxiliary guards.'})
    if changed:
        raise RuntimeError('Pinned tool sources changed; inspect summary before using results')


if __name__ == '__main__':
    main()
