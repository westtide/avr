#!/usr/bin/env python3
"""Finite expected payload contracts; no solver or existing artifact mutation."""
import json


def dense_value(address_values, element_width):
    return sum(value << (address * element_width) for address, value in enumerate(address_values))


def decode_dense(value, index_width, element_width):
    full_width = (1 << index_width) * element_width
    payload = format(value, f'0{full_width}b')
    assert len(payload) == full_width
    words = [int(payload[p:p + element_width], 2) for p in range(0, full_width, element_width)]
    return payload, words[::-1]


contracts = []
for domain, width, expected in (
    (1, 1, [1, 0]), (2, 1, [1, 0, 0, 0]), (2, 1, [0, 1, 0, 0]),
    (2, 8, [1, 0, 0, 0]), (2, 8, [0, 1, 0, 0]), (2, 8, [165, 0, 0, 0]),
):
    packed = dense_value(expected, width)
    bits, actual = decode_dense(packed, domain, width)
    assert actual == expected
    # Numeric mpz output has no leading zeros, but dense payloads are full width.
    assert len(bits) > len(format(packed, 'b'))
    contracts.append({'index_bits': domain, 'element_bits': width, 'packed': packed,
                      'dense_bits': bits, 'address_values': actual})
for width in (1, 8):
    broadcast = [1] * 4
    _, dense = decode_dense(1, 2, width)
    assert broadcast != dense
    contracts.append({'index_bits': 2, 'element_bits': width, 'element_init': 1,
                      'broadcast_address_values': broadcast, 'dense_value_1_address_values': dense})
print(json.dumps({'contracts': contracts, 'passed': len(contracts), 'solver_runs': 0}, indent=2))
