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
- Sub-hour demand for hourly pairs: `quarter_shares(four counts)` (each 15 minutes' share of the hour, () when
  even), `phf_quarters(phf)` and `phf_of(quarters)`. PHF = V / (4·V15) (HCM 2000 Eq 7-1, 7-2), 0.25–1: the
  busiest quarter carries 1/(4·PHF) of the hour and the other three share the rest evenly. The busiest is the
  second (`PEAK_QUARTER`): the first fills the network, the last two let it clear (the app's choice; the HCM
  defines only the peak). The simulation counts each minute's arrivals at its quarter's rate
  (`Simulation.rate_now`). A daily curve takes no PHF: its hours already are the profile.
