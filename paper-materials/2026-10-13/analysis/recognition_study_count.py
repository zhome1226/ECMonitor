"""Exploratory association between external recognition and supporting study count.

Each row is one standardized chemical identity. The negative binomial model
uses a log link; the reference recognition group is group 4. See README.md for
input definitions, priors, MCMC settings and execution instructions.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
from pathlib import Path
import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import beta

GROUP_CODES = {'formally_listed_or_prioritized': 0, 'surface_water_monitoring_concern': 1, 'drinking_water_candidate_or_monitoring_concern': 2, 'supporting_ecotoxicity_evidence_only': 3, 'no_identified_list_coverage': 4}
BROAD_CATEGORIES = {'Pharmaceuticals', 'Plant protection products', 'PFAS', 'Industrial chemicals'}

def load_data(path):
    with path.open(newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    if len(rows) != 386 or [int(r['row_order']) for r in rows] != list(range(1, 387)):
        raise ValueError('Expected 386 chemical identities in their supplied order')
    for r in rows:
        if int(r['group_code']) != GROUP_CODES[r['external_recognition_status']]:
            raise ValueError('Recognition group mismatch: ' + r['chemical_id'])
        category = r['display_category'] if r['display_category'] in BROAD_CATEGORIES else 'Other'
        if r['broad_category'] != category:
            raise ValueError('Broad category mismatch: ' + r['chemical_id'])
    groups = np.array([int(r['group_code']) for r in rows])
    studies = np.array([int(r['n_studies']) for r in rows])
    categories = np.array([r['broad_category'] for r in rows])
    depth = np.digitize(studies, [2, 5, 10])
    return groups, studies, depth, categories

def counts(groups, depth):
    out = np.zeros((5, 4), dtype=int)
    for g, d in zip(groups, depth):
        out[g, d] += 1
    return out

def dirichlet_summary(counts_matrix):
    result = {}
    for alpha in (1.0, 0.5, 0.1):
        for gi, row in enumerate(counts_matrix):
            for label, indexes in [('at least 2', [1, 2, 3]), ('at least 5', [2, 3])]:
                # A sum of components of a Dirichlet distribution is beta.
                a = float(row[indexes].sum() + alpha * len(indexes))
                b = float(row.sum() - row[indexes].sum() + alpha * (4 - len(indexes)))
                result[f'{alpha}|{gi}|{label}'] = (beta.ppf([.5, .025, .975], a, b) * 100).tolist()
            for di, c in enumerate(row):
                a = float(c + alpha)
                b = float(row.sum() - c + 3 * alpha)
                result[f'{alpha}|{gi}|depth{di}'] = (beta.ppf([.5, .025, .975], a, b) * 100).tolist()
    return result

def design(groups, categories, adjusted):
    # Tier 4 is the comparison group. Ordered coefficients: Tier 1, Tier 2
    # surface, Tier 2 drinking, Tier 3.
    x = [np.ones(len(groups))] + [(groups == i).astype(float) for i in range(4)]
    cat_levels = ['Industrial chemicals', 'PFAS', 'Pharmaceuticals', 'Plant protection products']
    if adjusted:
        x += [(categories == c).astype(float) for c in cat_levels]
    return np.column_stack(x)

def nlp(z, x, y, slope_sd):
    k = x.shape[1]
    coef = z[:k]
    log_shape = z[k]
    shape = np.exp(log_shape)
    eta = x @ coef
    mu = np.exp(np.clip(eta, -25, 25))
    ll = (gammaln(y + shape) - gammaln(shape) - gammaln(y + 1)
          + shape * (log_shape - np.log(shape + mu))
          + y * (eta - np.log(shape + mu)))
    prior = coef[0] ** 2 / (2 * 2.0 ** 2)
    prior += np.sum(coef[1:5] ** 2) / (2 * slope_sd ** 2)
    prior += np.sum(coef[5:] ** 2) / (2 * 1.0 ** 2)
    prior += log_shape ** 2 / (2 * 2.0 ** 2)
    return float(-ll.sum() + prior)

def finite_hessian(fun, x):
    eps = 1e-4
    n = len(x)
    h = np.empty((n, n))
    f0 = fun(x)
    for i in range(n):
        ei = np.eye(n)[i] * eps
        h[i, i] = (fun(x + ei) - 2 * f0 + fun(x - ei)) / eps ** 2
        for j in range(i + 1, n):
            ej = np.eye(n)[j] * eps
            h[i, j] = h[j, i] = (fun(x + ei + ej) - fun(x + ei - ej)
                                  - fun(x - ei + ej) + fun(x - ei - ej)) / (4 * eps ** 2)
    return h

def ess_1d(a):
    # Geyer's initial-positive-sequence estimate, pooled over two chains.
    nchain, ndraw = a.shape
    acfs = []
    for chain in a:
        c = chain - chain.mean()
        nfft = 1 << ((2 * ndraw - 1).bit_length())
        spec = np.fft.rfft(c, nfft)
        auto = np.fft.irfft(spec * np.conjugate(spec), nfft)[:ndraw]
        acfs.append(auto / auto[0])
    rho = np.mean(acfs, axis=0)
    pair_sum = rho[1::2][:len(rho[2::2])] + rho[2::2]
    good = np.flatnonzero(pair_sum <= 0)
    m = int(good[0]) if len(good) else len(pair_sum)
    tau = max(1.0, 1 + 2 * pair_sum[:m].sum())
    return float(nchain * ndraw / tau)

def split_rhat(a):
    half = a.shape[1] // 2
    split = np.concatenate([a[:, :half], a[:, -half:]], axis=0)
    means = split.mean(axis=1)
    within = split.var(axis=1, ddof=1).mean()
    between = half * means.var(ddof=1)
    return float(np.sqrt(((half - 1) * within / half + between / half) / within))

def sample_model(x, y, slope_sd, seed):
    k = x.shape[1]
    fun = lambda z: nlp(z, x, y, slope_sd)
    start = np.r_[np.log(y.mean()), np.zeros(k-1), 0.8]
    opt = minimize(fun, start, method='BFGS', options={'gtol': 1e-5, 'maxiter': 300})
    center = opt.x
    hess = finite_hessian(fun, center)
    eig, vec = np.linalg.eigh(hess)
    cov = (vec * (1 / np.maximum(eig, 1e-4))) @ vec.T
    root = np.linalg.cholesky(cov)
    rng = np.random.default_rng(seed)
    burn, retained, thinning = 12000, 8000, 4
    chains = np.empty((2, retained, k+1))
    acceptance = []
    for c in range(2):
        z = center + root @ rng.normal(size=k+1) * 0.15
        value = fun(z)
        scale = 2.38 / np.sqrt(k+1)
        accepts = 0
        accepted_window = 0
        written = 0
        total = burn + retained * thinning
        for it in range(total):
            cand = z + scale * (root @ rng.normal(size=k+1))
            candidate_value = fun(cand)
            if math.log(rng.random()) < value - candidate_value:
                z, value = cand, candidate_value
                if it >= burn:
                    accepts += 1
                accepted_window += 1
            if it < burn and (it + 1) % 500 == 0:
                rate = accepted_window / 500
                scale *= np.exp(np.clip((rate - 0.28) * 0.5, -0.15, 0.15))
                accepted_window = 0
            if it >= burn and (it - burn) % thinning == 0:
                chains[c, written] = z
                written += 1
        acceptance.append(accepts / (retained * thinning))
    ratios = np.exp(chains[:, :, 1:5])
    intervals = [np.quantile(ratios[:, :, j], [.5, .025, .975]).tolist() for j in range(4)]
    rhat = max(split_rhat(chains[:, :, j]) for j in range(k+1))
    ess = min(ess_1d(chains[:, :, j]) for j in range(k+1))
    return {
        'intervals': intervals,
        'acceptance': acceptance,
        'max_rhat': rhat,
        'min_ess': ess,
        'map_ratios': np.exp(center[1:5]).tolist(),
        'draws': ratios,
    }

def run(input_path, output_path):
    output_path.mkdir(exist_ok=True, parents=True)
    groups, studies, depth, categories = load_data(input_path)
    c = counts(groups, depth)
    if c.sum() != 386 or c.sum(axis=1).tolist() != [45, 16, 32, 112, 181]:
        raise AssertionError(c)
    payload = {'source_n': len(studies), 'counts': c.tolist(),
               'dirichlet': dirichlet_summary(c), 'models': {}}
    samples = {}
    for name, slope_sd, adjusted, seed in [
        ('status_sd1', 1.0, False, 20261002),
        ('status_sd2_5', 2.5, False, 20261003),
        ('adjusted', 1.0, True, 20261004),
    ]:
        x = design(groups, categories, adjusted)
        fit = sample_model(x, studies.astype(float), slope_sd, seed)
        samples[name] = fit.pop('draws')
        payload['models'][name] = fit
        print(name, np.round([v[0] for v in fit['intervals']], 3),
              'rhat', round(fit['max_rhat'], 4), 'ESS', round(fit['min_ess']), flush=True)
    (output_path/'model_results.json').write_text(json.dumps(payload, indent=2))
    np.savez_compressed(output_path/'posterior_rate_ratio_draws.npz', **samples)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path(__file__).parent/'input/chemical_identity_model_input.csv')
    parser.add_argument('--output', type=Path, default=Path(__file__).parent/'results')
    args = parser.parse_args()
    run(args.input, args.output)
