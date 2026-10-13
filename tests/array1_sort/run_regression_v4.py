#!/usr/bin/env python3
"""Small Array1 regression; fresh output, one CPU, 15 GiB, <=30s/model."""
import argparse
import csv
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[3]
HELPER = WORKSPACE / 'c2btor_vs_cpv/pono_static_mixed_matrix_20261010T040959.693061Z/frozen/cbmc/regression/goto-btor2/evaluate_mixed_array.py'
spec = importlib.util.spec_from_file_location('array1_limits', HELPER)
F = importlib.util.module_from_spec(spec); spec.loader.exec_module(F)
FAIL = re.compile(r'Assertion .*failed|\bf_err\b|type mismatch|type error|types? (?:do not|does not) match|segmentation fault|Aborted', re.I)


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write('\n')


def classify(engine, resource, directory):
    text = F.tail(resource['log'])
    if engine == 'avr':
        for file in directory.rglob('*.results'):
            text += '\n' + F.tail(file)
    if resource['status'] != 'Completed':
        return resource['status'], text
    if engine == 'avr':
        if 'AVR backend unsupported:' in text:
            return 'Unsupported', text
        if FAIL.search(text):
            return 'Failed', text
        values = re.findall(r'Verification result:\s*avr-([hv])\b', text)
        if len(set(values)) > 1:
            return 'Inconsistent', text
        if values:
            return {'h': 'UNSAT', 'v': 'SAT'}[values[-1]], text
        if re.search(r'\bf_to\b', text):
            return 'Timeout', text
        if re.search(r'\bf_mo\b', text):
            return 'OOM', text
    else:
        values = re.findall(r'^\s*(sat|unsat|unknown)\s*$', text, re.I | re.M)
        if values:
            return values[-1].upper(), text
    if re.search(r'unsupported|not supported|unimplemented|does not yet support', text, re.I):
        return 'Unsupported', text
    return 'UNKNOWN' if re.search(r'\bunknown\b', text, re.I) else 'Failed', text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--witness', action='store_true', help='Enable optional SAT witness generation and replay; default preserves historical flags.')
    parser.add_argument('--manifest', type=Path, default=HERE / 'cases_portable_payload.json')
    parser.add_argument('--engine', choices=('avr', 'pono-ind', 'pono-ic3sa', 'parser'), required=True)
    parser.add_argument('--avr-root', type=Path, default=WORKSPACE / 'checkers/avr')
    parser.add_argument('--avr-bin', type=Path, default=WORKSPACE / 'checkers/avr/build/bin')
    parser.add_argument('--backend', default='y2bt', choices=('y2bt', 'y2', 'bt'))
    parser.add_argument('--pono', type=Path, default=WORKSPACE / 'static-builds/pono-bzla-dev-static')
    parser.add_argument('--cpu', type=int, default=104)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--case', action='append')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.timeout <= 30 or args.cpu not in os.sched_getaffinity(0):
        parser.error('Require <=30 seconds and one available CPU')
    args.output = args.output.resolve(); args.output.mkdir(parents=True, exist_ok=False)
    args.manifest = args.manifest.resolve()
    args.avr_root = args.avr_root.resolve(); args.avr_bin = args.avr_bin.resolve(); args.pono = args.pono.resolve()
    cases = json.loads(args.manifest.read_text())
    cases = [c for c in cases if not args.case or c['case'] in args.case]
    tools = {'limits': HELPER, 'catbtor': WORKSPACE / 'c2btor_vs_cpv/pono_static_mixed_matrix_20261010T040959.693061Z/frozen/bin/catbtor',
             'btorsim': WORKSPACE / 'c2btor_vs_cpv/pono_static_mixed_matrix_20261010T040959.693061Z/frozen/bin/btorsim'}
    if args.engine == 'avr':
        tools.update(avr_py=args.avr_root / 'avr.py', avr_shell=args.avr_root / 'avr',
                     vwn=args.avr_bin / 'vwn', dpa=args.avr_bin / 'dpa', reach=args.avr_bin / ('reach_' + args.backend))
    elif args.engine != 'parser':
        tools['pono'] = args.pono
    save(args.output / 'provenance.json', {'utc': datetime.now(timezone.utc).isoformat(),
         'engine': args.engine, 'backend': args.backend, 'witness_enabled': args.witness, 'cpu': args.cpu, 'cores': 1, 'memory_gib': 15,
         'timeout_s': args.timeout, 'file_limit_gib': 1, 'cases': len(cases),
         'script_sha256': F.digest(Path(__file__)), 'manifest': str(args.manifest), 'manifest_sha256': F.digest(args.manifest),
         'tools': {name: {'path': str(path.resolve()), 'sha256': F.digest(path)} for name, path in tools.items()}})
    results = []
    fields = ['case', 'expected', 'result', 'passed', 'optional_feature', 'time_s', 'cpu_s', 'rss_MiB',
              'exit_code', 'diagnostics', 'model', 'model_sha256', 'log',
              'witness_status', 'witness', 'witness_sha256', 'witness_log', 'printed_verdict', 'printed_answer_matches']
    with (args.output / 'results.csv').open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fields, extrasaction='ignore'); writer.writeheader(); stream.flush()
        for case in cases:
            model = Path(case['model'])
            if not model.is_absolute():
                model = args.manifest.parent / model
            model = model.resolve()
            directory = args.output / case['case']; directory.mkdir()
            parsing = F.run_limited([tools['catbtor'], model], directory, 'parser', 30, 15, [args.cpu],
                                    cwd=WORKSPACE, stdout_discard=True, fsize_gib=1)
            if parsing['status'] != 'Completed' or parsing['exit_code']:
                raise RuntimeError('Invalid regression model: ' + case['case'])
            if args.engine == 'parser':
                resource, result, text = parsing, 'Parsed', ''
            else:
                if args.engine == 'avr':
                    command = [sys.executable, args.avr_root / 'avr.py', '--bt', model,
                               '-o', directory / 'avr_output', '-n', case['case'], '--bin', args.avr_bin,
                               '--backend', args.backend, '--timeout', str(max(1, int(args.timeout))), '--memout', '15360']
                    if args.witness:
                        command.append('--witness')
                    cwd = args.avr_root
                else:
                    engine = 'ind' if args.engine == 'pono-ind' else 'ic3sa'
                    command = [args.pono, '-e', engine, '--smt-solver', 'bzla', '-k', '100',
                               '--dump-btor2-witness', directory / 'pono.witness.txt', model]
                    cwd = WORKSPACE
                resource = F.run_limited(command, directory, 'solver', args.timeout, 15, [args.cpu], cwd=cwd, fsize_gib=1)
                result, text = classify(args.engine, resource, directory)
            raw_values = re.findall(r'Verification result:\s*avr-([hv])\b', text) if args.engine == 'avr' else []
            printed_verdict = ('Inconsistent' if len(set(raw_values)) > 1 else {'h': 'UNSAT', 'v': 'SAT'}[raw_values[-1]]) if raw_values else result
            witness_record = {'witness_status': 'NotRequested' if printed_verdict == 'SAT' else 'NotApplicable', 'witness': '', 'witness_sha256': '', 'witness_log': ''}
            if printed_verdict == 'SAT' and args.witness:
                witness_files = list(directory.rglob('cex.witness')) if args.engine == 'avr' else [directory / 'pono.witness.txt']
                witness_files = [path for path in witness_files if path.exists() and path.stat().st_size]
                witness_record['witness_status'] = 'Missing'
                if len(witness_files) == 1:
                    witness = witness_files[0]
                    replay = F.run_limited([tools['btorsim'], '-c', model, witness], directory, 'witness_replay', 10, 15, [args.cpu], cwd=WORKSPACE, fsize_gib=1)
                    status = 'Validated' if replay['status'] == 'Completed' and replay['exit_code'] == 0 else 'InvalidOrUnsupported'
                    witness_record.update(witness_status=status, witness=str(witness), witness_sha256=F.digest(witness), witness_log=replay['log'])
                elif len(witness_files) > 1:
                    witness_record['witness_status'] = 'Ambiguous'
            diagnostics = [line for line in text.splitlines() if FAIL.search(line) or re.search('unsupported|not supported', line, re.I)]
            record = {**case, **witness_record, 'model': str(model), 'result': result, 'passed': result == case['expected'] if args.engine != 'parser' else result == 'Parsed',
                      'printed_verdict': printed_verdict, 'printed_answer_matches': printed_verdict == case['expected'],
                      'model_sha256': F.digest(model), 'time_s': resource['time_s'], 'cpu_s': resource['cpu_s'],
                      'rss_MiB': resource['rss_MiB'], 'exit_code': resource['exit_code'], 'log': resource['log'],
                      'diagnostics': '\n'.join(diagnostics)[-8192:]}
            save(directory / 'result.json', record); writer.writerow(record); stream.flush(); results.append(record)
            print(json.dumps({k: record[k] for k in ('case', 'expected', 'result', 'passed', 'time_s', 'diagnostics')}), flush=True)
    save(args.output / 'summary.json', {'cases': len(results), 'passed': sum(r['passed'] for r in results),
         'printed_answers_matching': sum(r['printed_answer_matches'] for r in results),
         'reported_sat_witness_statuses': {status: sum(r['witness_status'] == status for r in results if r['printed_verdict'] == 'SAT') for status in sorted({r['witness_status'] for r in results if r['printed_verdict'] == 'SAT'})},
         'required_passed': sum(r['passed'] for r in results if not r['optional_feature']),
         'required_cases': sum(not r['optional_feature'] for r in results),
         'sat_witness_statuses': {status: sum(r['witness_status'] == status for r in results if r['result'] == 'SAT') for status in sorted({r['witness_status'] for r in results if r['result'] == 'SAT'})},
         'optional_results': [{k: r[k] for k in ('case', 'result', 'passed')} for r in results if r['optional_feature']],
         'mismatches': [{k: r[k] for k in ('case', 'expected', 'result', 'log')} for r in results if not r['passed']]})


if __name__ == '__main__':
    main()
