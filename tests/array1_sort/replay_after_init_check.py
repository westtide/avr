#!/usr/bin/env python3
"""Audit tiny fixtures: omit only proved redundant #0 array assignments in a copy."""
import argparse
import csv
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import signal
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[3]
FROZEN = WORKSPACE / 'c2btor_vs_cpv/pono_static_mixed_matrix_20261010T040959.693061Z/frozen'


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def known_initial_arrays(model):
    """Accept only scalar constant -> uniform Array initialization, no expression evaluation."""
    sorts, constants, states, initializers = {}, {}, [], {}
    for line in model.read_text().splitlines():
        fields = line.split(';', 1)[0].split()
        if not fields:
            continue
        node, op = int(fields[0]), fields[1]
        if op == 'sort':
            sorts[node] = ('bv', int(fields[3])) if fields[2] == 'bitvec' else ('array', int(fields[3]), int(fields[4]))
        elif op == 'state':
            states.append((node, int(fields[2])))
        elif op in ('zero', 'one', 'ones', 'const', 'constd', 'consth'):
            sort = int(fields[2])
            if sorts[sort][0] != 'bv':
                continue
            width = sorts[sort][1]
            if width > 64:
                continue
            value = {'zero': 0, 'one': 1, 'ones': (1 << width) - 1}.get(op)
            if value is None:
                value = int(fields[3], {'const': 2, 'constd': 10, 'consth': 16}[op]) & ((1 << width) - 1)
            constants[node] = (sort, value)
        elif op == 'init':
            state, rhs = int(fields[3]), int(fields[4])
            if state in initializers and initializers[state] != rhs:
                raise ValueError('Conflicting model initializers')
            initializers[state] = rhs
    result = {}
    for ordinal, (node, sort) in enumerate(states):
        meta = sorts[sort]
        if meta[0] != 'array' or node not in initializers:
            continue
        rhs = constants.get(initializers[node])
        if rhs is None or rhs[0] != meta[2]:
            continue
        domain, element = sorts[meta[1]], sorts[meta[2]]
        if domain[0] != 'bv' or element[0] != 'bv' or domain[1] > 8 or element[1] > 64:
            continue
        result[ordinal] = {'state_node': node, 'index_bits': domain[1], 'element_bits': element[1], 'value': rhs[1]}
    return result


def normalize(model, original):
    arrays = known_initial_arrays(model)
    lines = original.read_text().splitlines(keepends=True)
    section, kept, removed = None, [], []
    for number, line in enumerate(lines, 1):
        text = line.strip()
        if text.startswith('#') and text[1:].isdigit():
            section = ('states', int(text[1:]))
        elif text.startswith('@') and text[1:].isdigit():
            section = ('inputs', int(text[1:]))
        fields = text.split()
        if section == ('states', 0) and len(fields) >= 2 and fields[0].isdigit() and fields[1].startswith('['):
            state = int(fields[0])
            if state in arrays:
                spec = arrays[state]
                if len(fields) < 3 or not re.fullmatch(r'\[[01]+\]', fields[1]) or not re.fullmatch(r'[01]+', fields[2]):
                    raise ValueError(f'Non-concrete initialized-array assignment at line {number}')
                address, bits = fields[1][1:-1], fields[2]
                if len(address) != spec['index_bits'] or len(bits) != spec['element_bits']:
                    raise ValueError(f'Initialized-array assignment width mismatch at line {number}')
                if int(bits, 2) != spec['value']:
                    raise ValueError(f'Initialized-array assignment contradicts model init at line {number}')
                removed.append({'original_line': number, 'original_text': text, 'state_ordinal': state, 'model_init': spec})
                continue
        kept.append(line)
    if not removed:
        raise ValueError('No provably redundant initialized-array assignments')
    return ''.join(kept), removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--regression', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cpu', type=int, default=104)
    args = parser.parse_args()
    args.regression = args.regression.resolve(); args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    helper = FROZEN / 'cbmc/regression/goto-btor2/evaluate_mixed_array.py'
    spec = importlib.util.spec_from_file_location('init_check_limits', helper)
    F = importlib.util.module_from_spec(spec); spec.loader.exec_module(F)
    sim = FROZEN / 'bin/btorsim'
    save(args.output / 'provenance.json', {'utc': datetime.now(timezone.utc).isoformat(), 'script_sha256': F.digest(Path(__file__)),
         'regression': str(args.regression), 'regression_summary_sha256': F.digest(args.regression / 'summary.json'),
         'btorsim_sha256': F.digest(sim), 'resource_helper_sha256': F.digest(helper), 'cpu': args.cpu,
         'eligibility': 'Original replay SIGSEGV only; scalar constant uniform Array init; each explicit #0 value checked. Inputs and uninitialized arrays preserved.'})
    records = []
    for result_path in sorted(args.regression.glob('*/result.json')):
        result = json.loads(result_path.read_text())
        if result.get('result') != 'SAT' or result.get('witness_status') != 'InvalidOrUnsupported':
            continue
        resource_path = result_path.parent / 'witness_replay.resources.json'
        original_resource = json.loads(resource_path.read_text())
        if original_resource['status'] != 'Completed' or original_resource['exit_code'] != -signal.SIGSEGV:
            continue
        directory = args.output / result['case']; directory.mkdir()
        model, original = Path(result['model']), Path(result['witness'])
        if F.digest(model) != result['model_sha256'] or F.digest(original) != result['witness_sha256']:
            raise RuntimeError('Prior model/witness changed')
        record = {'case': result['case'], 'original_witness': str(original), 'original_sha256': F.digest(original),
                  'original_replay_exit_code': original_resource['exit_code'], 'original_replay_log': original_resource['log'],
                  'model_sha256': result['model_sha256'], 'status': 'UnsupportedInitCheck'}
        try:
            text, removed = normalize(model, original)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
            checked = directory / ('witness_init_checked_' + stamp + '.txt')
            with checked.open('x') as stream:
                stream.write(text)
            replay = F.run_limited([sim, '-c', model, checked], directory, 'init_checked_replay', 10, 15, [args.cpu], cwd=WORKSPACE, fsize_gib=1)
            record.update(status='ValidatedAfterInitCheck' if replay['status'] == 'Completed' and replay['exit_code'] == 0 else 'InvalidOrUnsupportedAfterInitCheck',
                          normalized_witness=str(checked), normalized_sha256=F.digest(checked), removed_entries=len(removed),
                          init_check=removed, replay_resource=replay, replay_log=replay['log'])
        except ValueError as error:
            record.update(error=str(error))
        save(directory / 'result.json', record); records.append(record)
        print(json.dumps({k: record[k] for k in ('case', 'status', 'original_replay_exit_code')}), flush=True)
    with (args.output / 'results.csv').open('x', newline='') as stream:
        fields = ['case', 'status', 'original_replay_exit_code', 'original_sha256', 'normalized_sha256', 'removed_entries', 'original_witness', 'normalized_witness', 'replay_log', 'error']
        writer = csv.DictWriter(stream, fields, extrasaction='ignore'); writer.writeheader(); writer.writerows(records)
    save(args.output / 'summary.json', {'eligible_replays': len(records),
         'validated_after_init_check': sum(r['status'] == 'ValidatedAfterInitCheck' for r in records),
         'statuses': {status: sum(r['status'] == status for r in records) for status in sorted({r['status'] for r in records})},
         'original_witnesses_or_results_modified': False, 'solver_runs': 0})


if __name__ == '__main__':
    main()
