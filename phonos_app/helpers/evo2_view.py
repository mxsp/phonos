"""Shape an Evo2 generation result for the job output page.

The worker stores NVIDIA's response as-is: ``sequence`` (usually only the
generated bases, sometimes seed + generated) and ``sampled_probs`` (the
probability the model gave each base it sampled, aligned with the generated
part). Older results may carry log-probabilities instead. Everything the page
shows is computed here once, so the template only renders.
"""
from __future__ import annotations

import math
from statistics import median
from typing import Any, Optional

LOW_PROB = 0.5  # below this a base counts as "low probability" on the page
BASES = ('A', 'C', 'G', 'T')


def _probabilities(result: dict) -> list[float]:
    raw = result.get('sampled_probs') or result.get('probs') or result.get('logprobs') or []
    values = [float(p) for p in raw if isinstance(p, (int, float)) and not isinstance(p, bool)]
    if len(values) != len(raw):
        return []
    # Log-probabilities are all <= 0; probabilities are in [0, 1].
    if values and all(p <= 0 for p in values):
        values = [math.exp(p) for p in values]
    return [min(1.0, max(0.0, p)) for p in values]


def _composition(seq: str) -> dict[str, Any]:
    counts = {b: seq.count(b) for b in BASES}
    total = len(seq)
    other = total - sum(counts.values())
    gc = counts['G'] + counts['C']
    return {
        'length': total,
        'counts': counts,
        'other': other,
        'pcts': {b: round(c / total * 100, 1) if total else 0.0 for b, c in counts.items()},
        'gc_pct': round(gc / total * 100, 1) if total else None,
    }


def _longest_homopolymer(seq: str) -> tuple[int, str, int]:
    """(length, base, 0-based start) of the longest single-base run."""
    best = (0, '', 0)
    run_start = 0
    for i in range(1, len(seq) + 1):
        if i == len(seq) or seq[i] != seq[run_start]:
            if i - run_start > best[0]:
                best = (i - run_start, seq[run_start], run_start)
            run_start = i
    return best


def build_evo2_view(result: Optional[dict], seed: Optional[str]) -> Optional[dict]:
    """Return the data the Evo2 output view renders, or None if there is no usable result."""
    if not isinstance(result, dict):
        return None
    full = str(result.get('sequence') or '').strip().upper()
    if not full:
        return None
    seed = str(seed or '').strip().upper()

    # Some responses repeat the seed in front of the generated bases.
    if seed and full.startswith(seed) and len(full) > len(seed):
        generated = full[len(seed):]
    else:
        generated = full

    probs = _probabilities(result)
    # Probabilities belong to the generated bases; drop any surplus so the
    # positions on the page never point past the sequence.
    probs = probs[:len(generated)]
    has_probs = bool(probs)

    stats: dict[str, Any] = {
        'seed_length': len(seed),
        'generated_length': len(generated),
        'total_length': len(seed) + len(generated),
        'seed': _composition(seed) if seed else None,
        'generated': _composition(generated),
    }
    run_len, run_base, run_start = _longest_homopolymer(generated)
    stats['longest_run'] = {'length': run_len, 'base': run_base, 'position': len(seed) + run_start + 1}

    lowest: list[dict] = []
    if has_probs:
        low = [p for p in probs if p < LOW_PROB]
        stats.update({
            'mean_prob': round(sum(probs) / len(probs) * 100, 1),
            'median_prob': round(median(probs) * 100, 1),
            'min_prob': round(min(probs) * 100, 1),
            'max_prob': round(max(probs) * 100, 1),
            'low_count': len(low),
            'low_pct': round(len(low) / len(probs) * 100, 1),
            'scored_count': len(probs),
        })
        for i in sorted(range(len(probs)), key=lambda k: probs[k])[:8]:
            pos = len(seed) + i + 1  # 1-based position in seed + generated
            lowest.append({
                'position': pos,
                'base': generated[i],
                'prob': round(probs[i] * 100, 1),
                'before': (seed + generated)[max(0, pos - 6):pos - 1],
                'after': (seed + generated)[pos:pos + 5],
            })

    return {
        'seed': seed,
        'generated': generated,
        'probs': [round(p, 4) for p in probs],
        'has_probs': has_probs,
        'low_threshold': LOW_PROB,
        'stats': stats,
        'lowest': lowest,
    }
