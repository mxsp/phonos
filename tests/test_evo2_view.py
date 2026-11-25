"""Tests for the Evo2 output view model (phonos_app.helpers.evo2_view).

The module is dependency-free, so it is loaded directly rather than through the
Flask app (same approach as test_model_io.py).
"""
from __future__ import annotations

import importlib.util
import math
import os

import pytest

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'phonos_app', 'helpers', 'evo2_view.py',
)
_spec = importlib.util.spec_from_file_location('phonos_evo2_view_under_test', _MODULE_PATH)
evo2_view = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evo2_view)
build = evo2_view.build_evo2_view


def test_generated_only_response_keeps_seed_separate():
    view = build({'sequence': 'TTGACC', 'sampled_probs': [0.9, 0.2, 0.5, 0.6, 0.1, 0.95]}, 'aaatt')
    assert view['seed'] == 'AAATT'
    assert view['generated'] == 'TTGACC'
    assert view['stats']['total_length'] == 11
    assert view['stats']['generated']['gc_pct'] == 50.0


def test_seed_repeated_in_front_is_stripped():
    view = build({'sequence': 'ATGCCCGGG', 'sampled_probs': None}, 'ATG')
    assert view['generated'] == 'CCCGGG'
    assert view['has_probs'] is False
    assert view['lowest'] == []


def test_logprobs_are_converted_and_clamped_to_generated_length():
    view = build({'sequence': 'ACG', 'logprobs': [-0.1, -2.0, -0.5, -0.3]}, '')
    assert view['probs'] == [round(math.exp(-0.1), 4), round(math.exp(-2.0), 4), round(math.exp(-0.5), 4)]


def test_low_probability_stats_and_lowest_positions_are_one_based_over_seed_plus_generated():
    view = build({'sequence': 'ACGT', 'sampled_probs': [0.9, 0.1, 0.45, 0.8]}, 'TT')
    stats = view['stats']
    assert stats['low_count'] == 2
    assert stats['low_pct'] == 50.0
    first = view['lowest'][0]
    assert first == {'position': 4, 'base': 'C', 'prob': 10.0, 'before': 'TTA', 'after': 'GT'}


def test_longest_homopolymer_position():
    view = build({'sequence': 'ACCCCGT', 'sampled_probs': []}, 'GG')
    assert view['stats']['longest_run'] == {'length': 4, 'base': 'C', 'position': 4}


@pytest.mark.parametrize('result', [None, {}, {'sequence': ''}, 'not a dict'])
def test_unusable_results_return_none(result):
    assert build(result, 'ACGT') is None


def test_non_numeric_probabilities_are_ignored():
    view = build({'sequence': 'ACGT', 'sampled_probs': [0.5, 'x', 0.2, 0.1]}, '')
    assert view['has_probs'] is False
