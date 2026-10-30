"""Statistics for the agent's tools (no Qt, standard library only), from research/statistics_catalogue.md.

- Distributions: Student's t (two-sided p and critical value at any confidence) and the χ² upper tail,
  from the regularized incomplete beta and gamma functions (continued fractions, Numerical Recipes).
- `paired`: the paired t interval of two scenarios run on the same seeds (common random numbers;
  McClave & Sincich §9.3), tighter than Welch's when the seeds make both runs alike.
- `replications`: runs needed for a mean within ±precision, N = (t·s / (precision·mean))² (FHWA Traffic
  Analysis Toolbox III, 2019, p89–91); over 20 suggests an unstable model or gridlock.
- `dispersion`: counts per interval against Poisson, the index of dispersion I = variance / mean and its
  χ² test (n − 1)·I ~ χ²(n − 1) (HCM 2000 p318's I; Washington et al. p654's overdispersion).
- `fit`: modelled against counted flows: GEH, RMSE, %RMSE, MAPE, the slope of counted on modelled
  through the origin (above 1: the model is low) and R².
- `logit`, `nested_logit`, `logsum`: multinomial and nested logit choice probabilities from utilities (Gumbel
  errors; Washington et al. ch. 13; Handbook of Transportation Science p22–26).
- `f_sf`, `anova`: one-way or randomized-block ANOVA (seeds as blocks; McClave & Sincich §10.2–10.4);
  `signed_rank`: Wilcoxon's signed-rank test, exact (Washington et al. p125–160); `holm`: Holm's adjusted p.
- `ols`: least squares with t, p, R², F (Washington et al. ch. 3); `poisson_regression`: log-linear count
  model by IRLS with an offset, NB2 overdispersion α by moments, Pearson dispersion (Washington et al.
  p631–662); `empirical_bayes` (Hauer: w = 1/(1 + αμ)).
- `kmeans`, `choose_k`: k-means++ on min–max-normalised attributes, k by the within/between variance ratio
  (FHWA Toolbox III 2019, p28–31); `representative_day` and `envelopes`: the 2019 calibration Criteria I–IV
  (p58–65).
- `holt`: Holt's linear exponential smoothing with its parameters fitted (Washington et al. p441–458);
  `mser`: the MSER-5 warm-up truncation (White 1997); `batch_means` (FHWA Traffic Simulation p25–29);
  `gini` (Geography of Transport Systems p646–649).
"""

from __future__ import annotations

import random
import statistics
from collections import Counter, defaultdict
from math import ceil, erfc, exp, inf, lgamma, log, sqrt

_TINY = 1e-300


def _betacf(a: float, b: float, x: float) -> float:
    """The incomplete beta function's continued fraction (modified Lentz)."""
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1)
    d = 1.0 / (d if abs(d) > _TINY else _TINY)
    h = d
    for m in range(1, 500):
        for aa in (m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
                   -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))):
            d = 1.0 + aa * d
            d = 1.0 / (d if abs(d) > _TINY else _TINY)
            c = 1.0 + aa / c
            c = c if abs(c) > _TINY else _TINY
            h *= d * c
        if abs(d * c - 1.0) < 1e-14:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta function I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = exp(lgamma(a + b) - lgamma(a) - lgamma(b) + a * log(x) + b * log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1 - x) / b


def gamma_q(a: float, x: float) -> float:
    """The regularized upper incomplete gamma function Q(a, x)."""
    if x <= 0:
        return 1.0
    front = exp(-x + a * log(x) - lgamma(a))
    if x < a + 1:  # the series for P, then 1 − P
        term = total = 1.0 / a
        for n in range(1, 1000):
            term *= x / (a + n)
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - front * total)
    b, c = x + 1 - a, 1.0 / _TINY  # the continued fraction for Q
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1.0 / (d if abs(d) > _TINY else _TINY)
        c = b + an / c
        c = c if abs(c) > _TINY else _TINY
        h *= d * c
        if abs(d * c - 1.0) < 1e-15:
            break
    return front * h


def t_p(t: float, df: float) -> float:
    """Two-sided p-value of Student's t with df degrees of freedom."""
    return betainc(df / 2, 0.5, df / (df + t * t))


def t_quantile(confidence: float, df: float) -> float:
    """The two-sided critical t: P(|T| ≤ t) = confidence (0.95 and 2 df: 4.303)."""
    low, high = 0.0, 1e7
    for _ in range(200):  # t_p falls as t rises
        middle = (low + high) / 2
        low, high = (middle, high) if t_p(middle, df) > 1 - confidence else (low, middle)
    return high


def chi2_sf(x: float, k: float) -> float:
    """P(χ²_k > x)."""
    return gamma_q(k / 2, x / 2)


def geh(modelled: float, counted: float) -> float:
    """The GEH statistic comparing hourly flows (UK DMRB practice: under 5 fits well)."""
    total = modelled + counted
    return sqrt(2 * (modelled - counted) ** 2 / total) if total > 0 else 0.0


def percentile(values, p: int) -> float | None:
    """The p-th percentile (1-99), linear between ranks (Excel's PERCENTILE.INC)."""
    values = list(values)
    if len(values) < 2:
        return values[0] if values else None
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]


def paired(base: dict, other: dict, confidence: float = 0.95) -> dict:
    """other − base over the seeds both have ({seed: value}): the mean difference, its interval at
    `confidence`, the p-value and `clear` when the interval leaves out zero. None where under 2 seeds pair."""
    seeds = [seed for seed in base if seed in other and base[seed] is not None and other[seed] is not None]
    if len(seeds) < 2:
        return {"diff": None, "ci": None, "p": None, "clear": None, "n": len(seeds)}
    differences = [other[seed] - base[seed] for seed in seeds]
    n, mean = len(seeds), statistics.fmean(differences)
    sd = statistics.stdev(differences)
    if sd == 0:
        return {"diff": mean, "ci": 0.0, "p": 0.0 if mean else 1.0, "clear": mean != 0, "n": n}
    half = t_quantile(confidence, n - 1) * sd / sqrt(n)
    return {"diff": mean, "ci": half, "p": t_p(mean / (sd / sqrt(n)), n - 1), "clear": abs(mean) > half, "n": n}


def replications(values, precision: float = 0.05, confidence: float = 0.95) -> int | None:
    """Runs needed for the mean to be within ±precision of itself at `confidence`, from these pilot runs."""
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return None
    mean, sd = statistics.fmean(values), statistics.stdev(values)
    if mean == 0:
        return None
    return max(1, ceil((t_quantile(confidence, len(values) - 1) * sd / (precision * abs(mean))) ** 2))


def dispersion(counts: list[int]) -> dict:
    """Counts per interval against Poisson: mean, variance, I = variance / mean, the two-sided p of
    (n − 1)·I ~ χ²(n − 1), and what that says (Poisson: I = 1; regular: below; bunched: above)."""
    n = len(counts)
    mean = statistics.fmean(counts) if counts else 0.0
    if n < 5 or mean == 0:
        return {"intervals": n, "mean": mean, "verdict": "too few counts to test"}
    variance = statistics.variance(counts)
    index = variance / mean
    upper = chi2_sf((n - 1) * index, n - 1)
    p = min(1.0, 2 * min(upper, 1 - upper))
    verdict = ("consistent with Poisson (random)" if p >= 0.05 else
               "more regular than Poisson" if index < 1 else "bunched: more variable than Poisson")
    return {"intervals": n, "mean": round(mean, 2), "variance": round(variance, 2), "index": round(index, 2),
            "p_poisson": round(p, 4), "verdict": verdict}


def fit(rows: list[tuple[float, float]]) -> dict:
    """(modelled, counted) pairs scored: GEH under 5 for 85 % of counts is the usual target (UK DMRB;
    FHWA Toolbox III 2004), RMSE and %RMSE, MAPE over non-zero counts, the slope of counted on
    modelled through the origin (1 = no bias, above 1: the model is low; HCM 2000 p135) and R² (precision)."""
    n = len(rows)
    if not n:
        raise ValueError("No counts to compare.")
    gehs = [geh(m, c) for m, c in rows]
    counted = sum(c for _, c in rows)
    rmse = sqrt(sum((m - c) ** 2 for m, c in rows) / n)
    nonzero = [(m, c) for m, c in rows if c]
    squares = sum(m * m for m, _ in rows)
    result = {"counts": n, "geh_under_5_percent": round(100 * sum(g < 5 for g in gehs) / n, 1),
              "meets_geh_target": sum(g < 5 for g in gehs) >= 0.85 * n, "rmse_vph": round(rmse, 1),
              "percent_rmse": round(100 * rmse / (counted / n), 1) if counted else None,
              "mape_percent": round(100 * statistics.fmean(abs(m - c) / c for m, c in nonzero), 1) if nonzero else None,
              "slope": round(sum(m * c for m, c in rows) / squares, 3) if squares else None}
    if n >= 3 and len({m for m, _ in rows}) > 1 and len({c for _, c in rows}) > 1:
        result["r2"] = round(statistics.correlation([m for m, _ in rows], [c for _, c in rows]) ** 2, 3)
    return result


def logit(utilities: dict) -> dict:
    """Multinomial logit: P_i = exp(U_i) / Σ exp(U_j)."""
    top = max(utilities.values())
    weights = {key: exp(value - top) for key, value in utilities.items()}
    total = sum(weights.values())
    return {key: weight / total for key, weight in weights.items()}


def reliability(trips) -> dict:
    """Travel-time reliability from trips (trip seconds, delay seconds), all from each trip's ratio of its time to
    its free-flow time: the travel time index (their mean), the planning time index (their 95th percentile; FHWA
    Toolbox III 2019, p92–93) and the buffer index, (95th percentile − mean) ÷ mean (FHWA's (TT95 − mean TT) ÷ mean
    TT, distance-normalised as the PTI is), one mean for both. Free-flow time = trip time − delay, the delay against
    the vehicle's own free speed (a bike's free flow is its own, not the limit). The spread is across one run's
    trips, not FHWA's across days."""
    pairs = [(trip, trip - lost) for trip, lost in trips if trip > 0 and trip - lost > 0]
    if not pairs:
        return {"tti": None, "pti": None, "bi": None}
    ratios = [t / f for t, f in pairs]
    p95, mean = percentile(ratios, 95), statistics.fmean(ratios)
    return {"tti": round(mean, 3), "pti": round(p95, 3),
            "bi": round((p95 - mean) / mean, 3)}


def logsum(utilities: dict) -> float:
    """ln Σ exp(U): the expected maximum utility (consumer surplus × −β_cost)."""
    top = max(utilities.values())
    return top + log(sum(exp(value - top) for value in utilities.values()))


def nested_logit(utilities: dict, nests: dict) -> tuple[dict, float]:
    """Nested logit: `nests` {name: (members, scale λ in (0, 1])}; members of no nest are a nest of their own
    at λ = 1 (plain logit then). P(m) = P(m | nest) · P(nest), P(m | n) ∝ exp(U_m / λ_n),
    P(n) ∝ exp(λ_n · IV_n), IV_n = ln Σ exp(U / λ_n). Returns (probabilities, logsum)."""
    groups = {name: (list(members), scale) for name, (members, scale) in nests.items()}
    nested = {member for members, _ in groups.values() for member in members}
    groups.update({key: ([key], 1.0) for key in utilities if key not in nested})
    inclusive = {name: logsum({m: utilities[m] / scale for m in members}) for name, (members, scale) in groups.items()}
    top = logsum({name: scale * inclusive[name] for name, (_, scale) in groups.items()})
    probabilities = {}
    for name, (members, scale) in groups.items():
        p_nest = exp(scale * inclusive[name] - top)
        for member in members:
            probabilities[member] = p_nest * exp(utilities[member] / scale - inclusive[name])
    return probabilities, top


def f_sf(f: float, d1: float, d2: float) -> float:
    """P(F_{d1,d2} > f)."""
    if f <= 0:
        return 1.0
    if f == inf:
        return 0.0
    return betainc(d2 / 2, d1 / 2, d2 / (d2 + d1 * f))


def anova(groups: dict) -> dict | None:
    """Do the scenarios differ at all? `groups` {scenario: {seed: value}}. Randomized block (seeds as blocks)
    when every scenario ran 2 or more of the same seeds, else one-way. F, its degrees of freedom and p."""
    groups = {name: {seed: v for seed, v in values.items() if v is not None} for name, values in groups.items()}
    if len(groups) < 2 or any(not values for values in groups.values()):
        return None
    k = len(groups)
    common = sorted(set.intersection(*(set(values) for values in groups.values())))
    if len(common) >= 2:
        b = len(common)
        cells = {(name, seed): groups[name][seed] for name in groups for seed in common}
        grand = statistics.fmean(cells.values())
        treatments = b * sum((statistics.fmean(groups[name][s] for s in common) - grand) ** 2 for name in groups)
        blocks = k * sum((statistics.fmean(groups[name][seed] for name in groups) - grand) ** 2 for seed in common)
        error = sum((v - grand) ** 2 for v in cells.values()) - treatments - blocks
        df1, df2, design = k - 1, (k - 1) * (b - 1), "randomized block (seeds as blocks)"
    else:
        values = [v for group in groups.values() for v in group.values()]
        grand = statistics.fmean(values)
        treatments = sum(len(g) * (statistics.fmean(g.values()) - grand) ** 2 for g in groups.values())
        error = sum((v - statistics.fmean(g.values())) ** 2 for g in groups.values() for v in g.values())
        df1, df2, design = k - 1, len(values) - k, "one-way"
    if df2 <= 0:
        return None
    error = max(error, 0.0)
    f = (treatments / df1) / (error / df2) if error > 1e-12 else (inf if treatments > 1e-12 else 0.0)
    return {"design": design, "f": f, "df": [df1, df2], "p": f_sf(f, df1, df2)}


def signed_rank(differences) -> float | None:
    """The Wilcoxon signed-rank test of paired differences, two-sided and exact (zeros dropped, ties at
    their mean rank); None with no non-zero difference."""
    d = [x for x in differences if x is not None and x != 0]
    n = len(d)
    if not n:
        return None
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0] * n  # doubled ranks: whole numbers even with ties
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        for m in range(i, j + 1):
            ranks[order[m]] = i + j + 2
        i = j + 1
    total, plus = sum(ranks), sum(r for r, x in zip(ranks, d) if x > 0)
    counts = {0: 1}  # the distribution of the doubled W+ when each sign is a coin toss
    for r in ranks:
        step = defaultdict(int)
        for value, ways in counts.items():
            step[value] += ways
            step[value + r] += ways
        counts = step
    far = abs(plus - total / 2)
    return min(1.0, sum(ways for value, ways in counts.items() if abs(value - total / 2) >= far - 1e-9) / 2 ** n)


def _inverse(matrix: list[list[float]]) -> list[list[float]]:
    """Gauss–Jordan with partial pivoting; ValueError if singular."""
    n = len(matrix)
    a = [list(row) + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("The predictors are collinear (or one never varies): drop one.")
        a[col], a[pivot] = a[pivot], a[col]
        lead = a[col][col]
        a[col] = [v / lead for v in a[col]]
        for r in range(n):
            if r != col and a[r][col]:
                factor = a[r][col]
                a[r] = [v - factor * w for v, w in zip(a[r], a[col])]
    return [row[n:] for row in a]


def _solve_weighted(X, weights, z):
    """(X'WX)⁻¹ X'Wz and (X'WX)⁻¹."""
    p = len(X[0])
    xtwx = [[sum(w * row[i] * row[j] for row, w in zip(X, weights)) for j in range(p)] for i in range(p)]
    inv = _inverse(xtwx)
    xtwz = [sum(w * row[i] * zi for row, w, zi in zip(X, weights, z)) for i in range(p)]
    return [sum(inv[i][j] * xtwz[j] for j in range(p)) for i in range(p)], inv


def ols(xs: list[list[float]], ys: list[float], names: list[str]) -> dict:
    """y = b0 + Σ b_j·x_j by least squares: each coefficient with its standard error, t and p; R², adjusted R²,
    F and its p, the residual SD."""
    n, p = len(ys), len(names) + 1
    if n <= p:
        raise ValueError(f"{n} observations can't fit {p} coefficients: give more.")
    X = [[1.0, *map(float, row)] for row in xs]
    b, inv = _solve_weighted(X, [1.0] * n, ys)
    fitted = [sum(bi * xi for bi, xi in zip(b, row)) for row in X]
    sse = sum((y - f) ** 2 for y, f in zip(ys, fitted))
    mean = statistics.fmean(ys)
    sst = sum((y - mean) ** 2 for y in ys)
    df = n - p
    s2 = sse / df
    rows = []
    for name, bi, i in zip(["intercept", *names], b, range(p)):
        se = sqrt(max(s2 * inv[i][i], 0.0))
        t = bi / se if se else inf
        rows.append({"term": name, "coefficient": bi, "se": se, "t": t, "p": t_p(t, df) if se else 0.0})
    r2 = 1 - sse / sst if sst else None
    result = {"n": n, "coefficients": rows, "r2": r2, "residual_sd": sqrt(s2)}
    if r2 is not None and p > 1:
        result["adjusted_r2"] = 1 - (1 - r2) * (n - 1) / df
        f = ((sst - sse) / (p - 1)) / s2 if s2 else inf
        result.update(f=f, f_p=f_sf(f, p - 1, df))
    return result


def poisson_regression(xs: list[list[float]], ys: list[float], offsets: list[float] | None = None) -> dict:
    """ln E[y] = offset + b0 + Σ b_j·x_j by maximum likelihood (iteratively reweighted least squares): the
    coefficients and standard errors, the fitted means μ, the Pearson dispersion (1 for Poisson; above:
    overdispersed, with its χ² p) and the negative binomial (NB2) α by moments, Σ((y − μ)² − y) / Σμ²."""
    n = len(ys)
    X = [[1.0, *map(float, row)] for row in xs]
    off = offsets or [0.0] * n
    p = len(X[0])
    if n <= p:
        raise ValueError(f"{n} sites can't fit {p} coefficients: give more.")
    if not any(ys):
        raise ValueError("Every count is zero: nothing to fit.")
    b = [log(statistics.fmean(ys) + 1e-9) - statistics.fmean(off)] + [0.0] * (p - 1)
    inv = None
    for _ in range(100):
        eta = [sum(bi * xi for bi, xi in zip(b, row)) + o for row, o in zip(X, off)]
        mu = [exp(max(-30.0, min(e, 30.0))) for e in eta]  # bounded: a site's mean never 0 (no division by it)
        z = [e - o + (y - m) / m for e, o, y, m in zip(eta, off, ys, mu)]
        new, inv = _solve_weighted(X, mu, z)
        moved = max(abs(u - v) for u, v in zip(new, b))
        b = new
        if moved < 1e-9:
            break
    mu = [exp(max(-30.0, min(sum(bi * xi for bi, xi in zip(b, row)) + o, 30.0))) for row, o in zip(X, off)]
    pearson = sum((y - m) ** 2 / m for y, m in zip(ys, mu))
    alpha = max(0.0, sum((y - m) ** 2 - y for y, m in zip(ys, mu)) / sum(m * m for m in mu))
    return {"coefficients": b, "se": [sqrt(max(inv[i][i], 0.0)) for i in range(p)], "mu": mu,
            "dispersion": pearson / (n - p), "dispersion_p": chi2_sf(pearson, n - p), "alpha": alpha}


def empirical_bayes(observed: float, predicted: float, alpha: float) -> float:
    """The Empirical Bayes estimate (Hauer): w·μ + (1 − w)·y with w = 1 / (1 + α·μ)."""
    w = 1 / (1 + alpha * predicted)
    return w * predicted + (1 - w) * observed


def _distance(a, b) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def kmeans(points: list[list[float]], k: int, seed: int = 1, restarts: int = 10) -> tuple[list[int], list[list[float]], float]:
    """k-means (Lloyd) from k-means++ starts, the best of `restarts`: (cluster of each point, centroids, the
    within-cluster sum of squares)."""
    rng, best = random.Random(seed), None
    for _ in range(restarts):
        centroids = [list(rng.choice(points))]
        while len(centroids) < k:
            weights = [min(_distance(p, c) for c in centroids) for p in points]
            centroids.append(list(rng.choices(points, weights=weights)[0] if sum(weights) else rng.choice(points)))
        labels = [0] * len(points)
        for _ in range(100):
            labels = [min(range(k), key=lambda c: _distance(p, centroids[c])) for p in points]
            moved = False
            for c in range(k):
                members = [p for p, label in zip(points, labels) if label == c]
                if members:
                    centre = [statistics.fmean(column) for column in zip(*members)]
                    moved |= centre != centroids[c]
                    centroids[c] = centre
            if not moved:
                break
        within = sum(_distance(p, centroids[label]) for p, label in zip(points, labels))
        if best is None or within < best[2]:
            best = (labels, centroids, within)
    return best


def choose_k(points: list[list[float]], seed: int = 1) -> int:
    """FHWA Toolbox III's stopping rule (2019, p31): k from 3 up to √(n/2), the one with the least
    within-cluster / between-cluster variance, each sum of squares over its degrees of freedom (n − k, k − 1;
    the Caliński–Harabasz ratio, inverted): raw sums would always favour the largest k."""
    n = len(points)
    top = max(3, int(sqrt(n / 2)))
    grand = [statistics.fmean(column) for column in zip(*points)]
    best, chosen = inf, min(3, n)
    for k in range(min(3, n), min(top, n - 1) + 1):
        labels, centroids, within = kmeans(points, k, seed)
        between = sum(labels.count(c) * _distance(centroids[c], grand) for c in range(k))
        ratio = (within / (n - k)) / (between / (k - 1)) if between else inf
        if ratio < best:
            best, chosen = ratio, k
    return chosen


def representative_day(days: list[list[float]]) -> int:
    """FHWA Toolbox III (2019, eq. 5–7): the day whose profile is closest to the mean profile, by the sum of
    squared deviations as shares of the mean."""
    means = [statistics.fmean(column) for column in zip(*days)]
    return min(range(len(days)), key=lambda i: sum(((m - x) / m) ** 2 for m, x in zip(means, days[i]) if m))


def envelopes(days: list[list[float]], simulated: list[float], kind: str, representative: int | None = None) -> dict:
    """FHWA Toolbox III's 2019 calibration test of one simulated profile against several observed days: the
    representative day r(t) ± σ(t) and ± 1.96σ(t) (σ over the days), and Criteria I (95 % inside the ~2σ band;
    under 20 intervals, all but one), II (two thirds and both critical intervals inside the 1σ band), III (mean
    absolute error from r within the BDAE threshold, the other days' mean absolute deviation from r) and IV
    (|mean error| within a third of it). Critical intervals: the highest travel time or flow, the lowest speed,
    and the next such non-adjacent one."""
    count = len(simulated)
    if len(days) < 3 or any(len(day) != count for day in days) or count < 3:
        raise ValueError(f"Give 3 or more observed days, each with one value per interval ({count} in the run).")
    rep = representative_day(days) if representative is None else int(representative)
    if not 0 <= rep < len(days):
        raise ValueError("representative_day is an index into days.")
    r = days[rep]
    sigma = [statistics.stdev(column) for column in zip(*days)]
    inside2 = [abs(s - o) <= 1.96 * g for s, o, g in zip(simulated, r, sigma)]
    inside1 = [abs(s - o) <= g for s, o, g in zip(simulated, r, sigma)]
    worst = sorted(range(count), key=lambda t: r[t] if kind == "speed" else -r[t])
    first = worst[0]
    second = next((t for t in worst[1:] if abs(t - first) > 1), None)
    critical = [t for t in (first, second) if t is not None]
    others = [day for i, day in enumerate(days) if i != rep]
    threshold = statistics.fmean(statistics.fmean(abs(o - x) for o, x in zip(day, r)) for day in others)
    error = [s - o for s, o in zip(simulated, r)]
    mae, bias = statistics.fmean(abs(e) for e in error), statistics.fmean(error)
    outside = inside2.count(False)
    criteria = {"I": outside <= (1 if count < 20 else 0.05 * count),
                "II": inside1.count(True) >= 2 * count / 3 and all(inside1[t] for t in critical),
                "III": mae <= threshold, "IV": abs(bias) <= threshold / 3}
    return {"representative_day": rep, "intervals": count, "inside_2_sigma": inside2.count(True),
            "inside_1_sigma": inside1.count(True), "critical_intervals": critical,
            "mean_abs_error": round(mae, 2), "mean_error": round(bias, 2), "bdae_threshold": round(threshold, 2),
            "criteria": criteria, "calibrated": all(criteria.values())}


def holt(series: list[float], horizon: int = 10) -> dict:
    """Holt's linear exponential smoothing, α and β fitted by the least one-step-ahead squared error (a 0.05
    grid), checked on the last fifth held out against the naive forecast (the last value), then refitted on
    all of it: the forecasts with rough 95 % intervals (± 1.96 · RMSE · √h)."""
    if len(series) < 8:
        raise ValueError("Forecasting needs 8 or more intervals of data.")
    grid = [i / 20 for i in range(1, 20)]

    def run(values, alpha, beta):
        level, trend, errors = values[0], values[1] - values[0], []
        for t in range(1, len(values)):
            guess = level + trend
            errors.append(values[t] - guess)
            new_level = alpha * values[t] + (1 - alpha) * guess
            trend = beta * (new_level - level) + (1 - beta) * trend
            level = new_level
        return level, trend, errors

    def fit(values):
        return min(((a, b) for a in grid for b in grid), key=lambda ab: sum(e * e for e in run(values, *ab)[2][1:]))

    cut = max(4, int(len(series) * 0.8))
    alpha, beta = fit(series[:cut])
    actual = series[cut:]
    held = run(series, alpha, beta)[2][cut - 1:]  # one step ahead through the held-out part
    naive = [series[t] - series[t - 1] for t in range(cut, len(series))]

    def mape(errors):
        pairs = [(e, a) for e, a in zip(errors, actual) if a]
        return round(100 * statistics.fmean(abs(e / a) for e, a in pairs), 1) if pairs else None

    alpha, beta = fit(series)
    level, trend, errors = run(series, alpha, beta)
    rmse = sqrt(statistics.fmean(e * e for e in errors[1:]))
    return {"alpha": alpha, "beta": beta, "holdout_mape": mape(held), "naive_mape": mape(naive),
            "rmse": round(rmse, 2), "forecast": [{"step": h, "value": round(level + h * trend, 2),
                                                   "low": round(level + h * trend - 1.96 * rmse * sqrt(h), 2),
                                                   "high": round(level + h * trend + 1.96 * rmse * sqrt(h), 2)}
                                                  for h in range(1, horizon + 1)]}


def mser(series: list[float], batch: int = 5) -> int:
    """The MSER-5 warm-up (White 1997): the number of leading values to drop, in whole batches of 5, that
    minimises the variance of the mean of what is left (searched over the first half)."""
    means = [statistics.fmean(series[i:i + batch]) for i in range(0, len(series) - batch + 1, batch)]
    if len(means) < 4:
        return 0

    def score(d):
        rest = means[d:]
        centre = statistics.fmean(rest)
        return sum((x - centre) ** 2 for x in rest) / len(rest) ** 2

    return min(range(len(means) // 2), key=score) * batch


def batch_means(series: list[float], batches: int = 10) -> dict | None:
    """One long run's mean with a 95 % interval from batch means (batches − 1 df), and the batch means' lag-1
    autocorrelation (near 0: batches long enough to be independent)."""
    size = len(series) // batches
    if size < 1 or batches < 3:
        return None
    means = [statistics.fmean(series[i * size:(i + 1) * size]) for i in range(batches)]
    mean, sd = statistics.fmean(means), statistics.stdev(means)
    centred = [m - mean for m in means]
    lag = sum(a * b for a, b in zip(centred, centred[1:])) / sum(c * c for c in centred) if sd else 0.0
    return {"mean": round(mean, 2), "ci95": round(t_quantile(0.95, batches - 1) * sd / sqrt(batches), 2),
            "batches": batches, "batch_minutes": size, "lag1_autocorrelation": round(lag, 2)}


def gini(values) -> float | None:
    """The Gini coefficient: 0 when every value is equal, towards 1 when one holds it all."""
    values = sorted(v for v in values if v is not None)
    n, total = len(values), sum(values)
    if not n or not total:
        return None
    return sum((2 * i - n + 1) * v for i, v in enumerate(values)) / (n * total)


def normal_sf(z: float) -> float:
    """P(Z > z) for the standard normal."""
    return 0.5 * erfc(z / sqrt(2))


def holm(ps: list) -> list:
    """Holm's step-down adjustment of p-values tested together (the family-wise error held at α; McClave &
    Sincich §10.3's Bonferroni, made uniformly more powerful): the i-th smallest p times (m − i + 1), kept
    non-decreasing, at most 1. None stays None."""
    order = sorted((p, i) for i, p in enumerate(ps) if p is not None)
    adjusted, running = list(ps), 0.0
    for rank, (p, i) in enumerate(order):
        running = max(running, min(1.0, (len(order) - rank) * p))
        adjusted[i] = running
    return adjusted


def _ranks(values: list[float]) -> list[float]:
    """Each value's rank, 1 for the smallest, ties at their mean rank."""
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for m in range(i, j + 1):
            ranks[order[m]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _ties(ranks: list[float]) -> float:
    """The sum of t³ − t over each group of t tied ranks."""
    return sum(t ** 3 - t for t in Counter(ranks).values() if t > 1)


def friedman(groups: dict) -> dict | None:
    """Friedman's test: do the scenarios differ, ranked within each seed (blocks)? `groups` {scenario: {seed:
    value}}, on the seeds every scenario ran. Q = [12/(b·k·(k + 1))·ΣR_j² − 3b(k + 1)] / (1 − Σ(t³ − t)/(b(k³ − k)))
    ~ χ²(k − 1) (Friedman 1937; Conover, Practical Nonparametric Statistics, 1999, §5.8). A rank test: no
    normality assumed, so it holds for skewed figures and few seeds."""
    groups = {name: {s: v for s, v in values.items() if v is not None} for name, values in groups.items()}
    k = len(groups)
    common = sorted(set.intersection(*(set(v) for v in groups.values()))) if k >= 2 else []
    b = len(common)
    if k < 2 or b < 2:
        return None
    sums, tie_sum = defaultdict(float), 0.0
    for seed in common:
        ranks = _ranks([groups[name][seed] for name in groups])
        tie_sum += _ties(ranks)
        for name, rank in zip(groups, ranks):
            sums[name] += rank
    q = 12 / (b * k * (k + 1)) * sum(r * r for r in sums.values()) - 3 * b * (k + 1)
    correction = 1 - tie_sum / (b * (k ** 3 - k))
    mean_ranks = {name: round(total / b, 2) for name, total in sums.items()}
    if correction <= 0:
        return {"q": 0.0, "df": k - 1, "p": 1.0, "seeds": b, "mean_ranks": mean_ranks}
    q /= correction
    return {"q": round(q, 3), "df": k - 1, "p": chi2_sf(q, k - 1), "seeds": b, "mean_ranks": mean_ranks}


def kruskal_wallis(groups: dict) -> dict | None:
    """Kruskal–Wallis: do independent groups differ? `groups` {name: [values]}. H = [12/(N(N + 1))·ΣR_j²/n_j −
    3(N + 1)] / (1 − Σ(t³ − t)/(N³ − N)) ~ χ²(k − 1) (Washington et al. §2.5; Conover §5.2)."""
    groups = {name: [v for v in values if v is not None] for name, values in groups.items()}
    groups = {name: values for name, values in groups.items() if values}
    every = [v for values in groups.values() for v in values]
    n, k = len(every), len(groups)
    if k < 2 or n <= k:
        return None
    ranks = _ranks(every)
    sums, at = {}, 0
    for name, values in groups.items():
        sums[name] = sum(ranks[at:at + len(values)])
        at += len(values)
    h = 12 / (n * (n + 1)) * sum(sums[name] ** 2 / len(values) for name, values in groups.items()) - 3 * (n + 1)
    correction = 1 - _ties(ranks) / (n ** 3 - n)
    h = h / correction if correction > 0 else 0.0
    return {"h": round(h, 3), "df": k - 1, "p": chi2_sf(h, k - 1)}


def mann_whitney(a: list[float], b: list[float]) -> dict | None:
    """Mann–Whitney U of two independent samples, two-sided by the normal approximation with the tie and
    continuity corrections (Washington et al. §2.5)."""
    a, b = [v for v in a if v is not None], [v for v in b if v is not None]
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return None
    ranks = _ranks(a + b)
    u = sum(ranks[:n1]) - n1 * (n1 + 1) / 2
    n = n1 + n2
    variance = n1 * n2 / 12 * ((n + 1) - _ties(ranks) / (n * (n - 1)))
    if variance <= 0:
        return {"u": u, "p": 1.0}
    z = (abs(u - n1 * n2 / 2) - 0.5) / sqrt(variance)
    return {"u": u, "z": round(z, 3), "p": min(1.0, 2 * normal_sf(max(z, 0.0)))}


def paired_power(sd: float, difference: float, n: int, alpha: float = 0.05) -> float:
    """Power of the two-sided paired t-test with n pairs to find a true mean difference `difference` when the
    differences' SD is `sd`: exact, P(|T'| > t_crit) for the noncentral t T' = (Z + δ)/S, δ = |difference|·√n/sd,
    S = √(χ²_ν/ν), ν = n − 1, by Simpson's rule over S's density (G*Power gives 0.401 at n 5, δ/√n 1; the
    shifted central t had 0.31)."""
    if n < 2:
        return 0.0
    if sd <= 0:
        return 1.0 if difference else 0.0
    nu, critical, shift = n - 1, t_quantile(1 - alpha, n - 1), abs(difference) * sqrt(n) / sd
    top, steps = 1 + 12 / sqrt(2 * nu), 2000
    log_norm = nu / 2 * log(nu) - (nu / 2 - 1) * log(2) - lgamma(nu / 2)

    def at(s: float) -> float:
        density = exp(log_norm + (nu - 1) * log(s) - nu * s * s / 2) if s > 0 else (1.0 if nu == 1 else 0.0) * exp(log_norm)
        return density * (normal_sf(critical * s - shift) + normal_sf(critical * s + shift))

    h = top / steps
    total = at(0.0) + at(top) + sum((4 if i % 2 else 2) * at(i * h) for i in range(1, steps))
    return min(1.0, total * h / 3)


def seeds_for_power(sd: float, difference: float, power: float = 0.8, alpha: float = 0.05, most: int = 1000) -> int | None:
    """The fewest pairs (seeds) with which the paired t-test finds `difference` with this power; None beyond `most`."""
    if not difference:
        return None
    for n in range(2, most + 1):
        if paired_power(sd, difference, n, alpha) >= power:
            return n
    return None


def detectable(sd: float, n: int, power: float = 0.8, alpha: float = 0.05) -> float | None:
    """The smallest mean difference n pairs find with this power: (t_{α/2} + t_β)·sd/√n (McClave & Sincich §8.7)."""
    if n < 2 or sd < 0:
        return None
    return (t_quantile(1 - alpha, n - 1) + t_quantile(1 - 2 * (1 - power), n - 1)) * sd / sqrt(n)


def all_pairs(groups: dict, higher_is_better: bool | None = None, confidence: float = 0.95) -> dict | None:
    """Every scenario against every other: the paired t interval over the seeds both ran at `confidence`,
    Bonferroni-widened for the m = k(k − 1)/2 comparisons (1 − α/m; McClave & Sincich §10.3), with Wilcoxon's
    signed-rank p; then the scenarios ranked by mean with letters, a letter shared by those no comparison tells
    apart (a compact letter display, Piepho 2004). `groups` {scenario: {seed: value}}."""
    names = [name for name, values in groups.items() if any(v is not None for v in values.values())]
    k = len(names)
    if k < 2:
        return None
    m = k * (k - 1) // 2
    level = 1 - (1 - confidence) / m
    means = {name: statistics.fmean(v for v in groups[name].values() if v is not None) for name in names}
    pairs, apart = [], set()
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            found = paired(groups[first], groups[second], level)
            if found["diff"] is None:
                continue
            shared = [s for s in groups[first] if s in groups[second]
                      and None not in (groups[first][s], groups[second][s])]
            pairs.append({"a": first, "b": second, "b_minus_a": found["diff"], "ci": found["ci"], "p": found["p"],
                          "clear": found["clear"], "seeds": found["n"],
                          "wilcoxon_p": signed_rank([groups[second][s] - groups[first][s] for s in shared])})
            if found["clear"]:
                apart.add(frozenset((first, second)))
    order = sorted(names, key=lambda name: means[name], reverse=bool(higher_is_better))
    runs: list[tuple[int, int]] = []  # maximal runs of neighbours (by mean) no two of which differ clearly
    start = 0
    for end in range(len(order)):
        while any(frozenset((order[i], order[end])) in apart for i in range(start, end)):
            start += 1
        if runs and runs[-1][0] == start:
            runs[-1] = (start, end)
        else:
            runs.append((start, end))
    letters = {name: "" for name in order}
    for index, (low, high) in enumerate(runs):
        for name in order[low:high + 1]:
            letters[name] += "abcdefghijklmnopqrstuvwxyz"[index % 26]
    return {"comparisons": m, "confidence_each": round(level, 4), "pairs": pairs,
            "ranked": [{"scenario": name, "mean": means[name], "group": letters[name]} for name in order]}


def two_way_anova(cells: dict) -> dict | None:
    """Two-factor ANOVA with replication (McClave & Sincich §10.5; Snell, Applied Statistics, Examples J-L): `cells`
    {(level of A, level of B): [values, one a seed]}, balanced (the same count in every cell, the fewest kept). Main
    effects of A and B and their interaction, each F against the within-cell error, its df and p. With one value a
    cell the interaction is the error, so it isn't tested. None when a level has no cell or fewer than 2 levels."""
    a_levels = list(dict.fromkeys(a for a, _ in cells))
    b_levels = list(dict.fromkeys(b for _, b in cells))
    if len(a_levels) < 2 or len(b_levels) < 2 or any((a, b) not in cells for a in a_levels for b in b_levels):
        return None
    n = min(len([v for v in cells[key] if v is not None]) for key in cells)
    if n < 1:
        return None
    data = {key: [v for v in values if v is not None][:n] for key, values in cells.items()}
    a, b = len(a_levels), len(b_levels)
    grand = statistics.fmean(v for values in data.values() for v in values)
    mean_a = {x: statistics.fmean(v for y in b_levels for v in data[(x, y)]) for x in a_levels}
    mean_b = {y: statistics.fmean(v for x in a_levels for v in data[(x, y)]) for y in b_levels}
    mean_ab = {key: statistics.fmean(values) for key, values in data.items()}
    ss_a = b * n * sum((m - grand) ** 2 for m in mean_a.values())
    ss_b = a * n * sum((m - grand) ** 2 for m in mean_b.values())
    ss_ab = n * sum((mean_ab[(x, y)] - mean_a[x] - mean_b[y] + grand) ** 2 for x in a_levels for y in b_levels)
    ss_e = sum((v - mean_ab[key]) ** 2 for key, values in data.items() for v in values)
    df_a, df_b, df_ab, df_e = a - 1, b - 1, (a - 1) * (b - 1), a * b * (n - 1)
    if n == 1:  # no replication: the interaction is the error
        ss_e, df_e, ss_ab = ss_ab, df_ab, None

    def test(ss, df):
        if ss is None or df_e <= 0:
            return None
        f = (ss / df) / (ss_e / df_e) if ss_e > 1e-12 else (inf if ss > 1e-12 else 0.0)
        return {"f": f, "df": [df, df_e], "p": f_sf(f, df, df_e)}

    return {"replicates": n, "A": test(ss_a, df_a), "B": test(ss_b, df_b), "interaction": test(ss_ab, df_ab),
            "means": {"A": mean_a, "B": mean_b, "cells": {f"{x} | {y}": m for (x, y), m in mean_ab.items()}}}
