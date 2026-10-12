"""Daily demand profiles: how one day's trips spread over 24 hours (no Qt).

A profile is a curve of relative level over the day, hour 0 to 24. It is shaped one of two ways:
- "points": control points joined by a monotone cubic (PCHIP, Fritsch-Carlson), like an
  image editor's Curves tool. It never overshoots between points, so it can't dip below
  zero or rise above the highest point by itself.
- "poly": a polynomial in t = hour / 24, level = a0 + a1 t + ... + an t^n, degree 2-6.
  Where it goes negative, the level counts as zero.
Each hour's share of the day is the curve's average level over that hour, normalised so
the 24 shares add up to 1. Placeholder: the OD matrix isn't saved or simulated yet.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

HOURS = 24
ARRIVAL_PATTERNS = ["Random", "Poisson", "Binomial", "Negative binomial"]  # per OD pair
DEGREES = (2, 3, 4, 5, 6)
MIN_GAP_H = 0.25  # control points stay at least this far apart in time
PEAK_QUARTER = 1  # a peak-hour factor's busiest 15 minutes: the hour's second (the first fills the network)
# A weekday with a morning and an evening peak: a starting shape to tune.
WEEKDAY = [(0.0, 0.06), (3.0, 0.03), (6.0, 0.35), (8.0, 1.0), (10.0, 0.55), (13.0, 0.62),
           (17.0, 0.95), (19.0, 0.5), (22.0, 0.18), (24.0, 0.06)]


@dataclass
class DailyProfile:
    kind: str = "points"  # "points" or "poly"
    points: list[tuple[float, float]] = field(default_factory=lambda: list(WEEKDAY))  # (hour, level)
    coefficients: list[float] = field(default_factory=list)  # a0..an, for "poly"

    def level(self, hour: float) -> float:
        if self.kind == "poly":
            t = hour / HOURS
            value = sum(a * t ** power for power, a in enumerate(self.coefficients))
        else:
            value = pchip(self.points, hour)
        return max(0.0, value)

    def hourly_levels(self, samples: int = 12) -> list[float]:
        """The curve's average level in each hour of the day."""
        return [sum(self.level(hour + (i + 0.5) / samples) for i in range(samples)) / samples
                for hour in range(HOURS)]

    def shares(self) -> list[float]:
        """Each hour's share of the day's trips; even shares if the curve is zero all day."""
        levels = self.hourly_levels()
        total = sum(levels)
        return [level / total for level in levels] if total > 0 else [1 / HOURS] * HOURS

    def as_polynomial(self, degree: int) -> DailyProfile:
        """The least-squares polynomial of this degree closest to the current curve."""
        samples = [(i / 96, self.level(i * HOURS / 96)) for i in range(97)]
        return DailyProfile("poly", [], fit_polynomial(samples, degree))

    def as_points(self, count: int = 9) -> DailyProfile:
        """Control points sampled from the current curve, to tune by hand."""
        hours = [i * HOURS / (count - 1) for i in range(count)]
        return DailyProfile("points", [(hour, self.level(hour)) for hour in hours])


def quarter_shares(values) -> tuple[float, ...]:
    """Hourly demand's four 15-minute counts (or shares) as the hour's shares, adding up to 1; () for an even hour.
    ValueError if they aren't four numbers, none negative, some above zero."""
    try:
        values = [float(v) for v in values]
    except (TypeError, ValueError):
        raise ValueError("quarters must be four numbers, one per 15 minutes of the hour.") from None
    if len(values) != 4 or min(values) < 0 or not 0 < sum(values) < float("inf"):
        raise ValueError("quarters must be four numbers, one per 15 minutes of the hour, none below zero.")
    shares = tuple(v / sum(values) for v in values)
    return () if max(shares) - min(shares) < 1e-9 else shares


def phf_quarters(phf: float) -> tuple[float, ...]:
    """The hour's 15-minute shares for a peak-hour factor PHF = V / (4·V15) (HCM 2000 Eq 7-2), 0.25 to 1: the busiest
    quarter (PEAK_QUARTER) carries 1/(4·PHF) of the hour, the other three share the rest evenly; () for 1."""
    if not 0.25 <= phf <= 1:
        raise ValueError("peak_hour_factor must be 0.25 to 1.")
    peak = 1 / (4 * phf)
    return quarter_shares([peak if quarter == PEAK_QUARTER else (1 - peak) / 3 for quarter in range(4)])


def phf_of(quarters) -> float:
    """The peak-hour factor of an hour's 15-minute shares (1 for an even hour)."""
    return 1 / (4 * max(quarters)) if quarters else 1.0


def pchip(points: list[tuple[float, float]], x: float) -> float:
    """Monotone cubic through (x, y) points sorted by x: each piece stays between its two
    points' values, so the curve has no bumps the points don't have."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    if len(points) == 1 or x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    widths = [b - a for a, b in zip(xs, xs[1:])]
    slopes = [(b - a) / width for a, b, width in zip(ys, ys[1:], widths)]
    tangents = [slopes[0], *([0.0] * (len(points) - 2)), slopes[-1]]
    for i in range(1, len(points) - 1):
        if slopes[i - 1] * slopes[i] > 0:  # weighted harmonic mean keeps it monotone
            w1, w2 = 2 * widths[i] + widths[i - 1], widths[i] + 2 * widths[i - 1]
            tangents[i] = (w1 + w2) / (w1 / slopes[i - 1] + w2 / slopes[i])
    i = min(bisect_right(xs, x) - 1, len(points) - 2)
    t, width = (x - xs[i]) / widths[i], widths[i]
    return ((2 * t ** 3 - 3 * t ** 2 + 1) * ys[i] + (t ** 3 - 2 * t ** 2 + t) * width * tangents[i]
            + (-2 * t ** 3 + 3 * t ** 2) * ys[i + 1] + (t ** 3 - t ** 2) * width * tangents[i + 1])


def fit_polynomial(samples: list[tuple[float, float]], degree: int) -> list[float]:
    """Coefficients a0..a_degree of the least-squares polynomial through (t, y) samples,
    from the normal equations (fine for degree <= 6 with t in [0, 1])."""
    size = degree + 1
    matrix = [[sum(t ** (row + column) for t, _ in samples) for column in range(size)] for row in range(size)]
    vector = [sum(y * t ** row for t, y in samples) for row in range(size)]
    return _solve(matrix, vector)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting."""
    size = len(vector)
    rows = [row[:] + [value] for row, value in zip(matrix, vector)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(rows[r][column]))
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for r in range(column + 1, size):
            factor = rows[r][column] / rows[column][column]
            rows[r] = [a - factor * b for a, b in zip(rows[r], rows[column])]
    solution = [0.0] * size
    for r in reversed(range(size)):
        solution[r] = (rows[r][size] - sum(rows[r][c] * solution[c] for c in range(r + 1, size))) / rows[r][r]
    return solution
