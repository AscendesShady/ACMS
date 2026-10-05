# daily_profile.py: how a day's trips spread over 24 hours (no Qt)

- `DailyProfile(kind, points, coefficients)`:
  - `kind="points"`: control points joined by a monotone cubic (`pchip`, Fritsch–Carlson). It passes
    through every point and never overshoots between them, so it can't dip below zero on its own.
  - `kind="poly"`: `level = a0 + a1·t + … + an·tⁿ` with `t = hour / 24`, degree 2–6. Negative
    parts count as zero.
- `level(hour)`, `hourly_levels()` (average level per hour) and `shares()` (each hour's share of
  the day; they sum to 1, or are even if the curve is zero all day).
- `as_polynomial(degree)`: a least-squares fit of the current curve (`fit_polynomial`: normal
  equations, Gaussian elimination).
- `as_points()`: 9 points sampled from the curve.
- `WEEKDAY`: the default shape, with a morning peak around 08:00 and an evening peak around 17:00.
  It is illustrative, not measured data.
