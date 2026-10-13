"""Check generated summaries against the archived Table S6 precision."""
import csv
import json
from pathlib import Path

BASE = Path(__file__).parent
result = json.loads((BASE/'results/model_results.json').read_text())
expected = list(csv.DictReader((BASE/'reference_results/table_s6.csv').open(newline='')))
observed = []
for alpha, estimands in [(1.0, ['at least 2', 'at least 5']), (.5, ['at least 5']), (.1, ['at least 5'])]:
    for est in estimands:
        for gi in range(5):
            med, lo, hi = result['dirichlet'][f'{alpha}|{gi}|{est}']
            observed.append(f'{med:.1f} ({lo:.1f}–{hi:.1f})')
for name in ['status_sd1', 'status_sd2_5', 'adjusted']:
    for med, lo, hi in result['models'][name]['intervals']:
        observed.append(f'{med:.3f} ({lo:.3f}–{hi:.3f})')
for name in ['status_sd1', 'status_sd2_5', 'adjusted']:
    m = result['models'][name]
    observed.append(f"{m['max_rhat']:.3f}; {m['min_ess']:.1f}")
target = [r['Posterior median (95% credible interval) or diagnostic'] for r in expected]
differences = [(i+1, t, o) for i, (t, o) in enumerate(zip(target, observed)) if t != o]
if differences:
    for i, t, o in differences:
        print(f'Row {i}: archived={t}, reproduced={o}')
    raise SystemExit('Differences found; check package versions and numerical platform.')
print('All 35 Table S6 summary and diagnostic rows match at reported precision.')
