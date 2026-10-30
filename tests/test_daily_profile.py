"""Daily demand profiles. Run with `python tests/test_daily_profile.py` (or the test monitor)."""

from traffic_simulator.daily_profile import HOURS, WEEKDAY, DailyProfile, fit_polynomial, pchip


def test_curve_passes_through_its_points_without_overshoot():
    for x, y in WEEKDAY:
        assert abs(pchip(WEEKDAY, x) - y) < 1e-12
    samples = [pchip(WEEKDAY, i / 10) for i in range(241)]
    assert min(samples) >= 0.03 - 1e-12 and max(samples) <= 1.0 + 1e-12  # no dips or bumps of its own
    for (x0, y0), (x1, y1) in zip(WEEKDAY, WEEKDAY[1:]):
        inside = [pchip(WEEKDAY, x0 + (x1 - x0) * k / 20) for k in range(21)]
        assert min(inside) >= min(y0, y1) - 1e-12 and max(inside) <= max(y0, y1) + 1e-12


def test_a_curve_with_two_points_at_one_hour_is_refused():
    from traffic_simulator.daily_profile import from_record

    try:
        from_record({"kind": "points", "points": [[0, 1], [8, 1], [8, 2], [24, 1]]})
    except ValueError as exc:
        assert "in order" in str(exc)
    else:
        raise AssertionError("two points at 08:00 would divide by zero in pchip")
    assert len(from_record({"kind": "points", "points": [[0, 1], [8, 2], [24, 1]]}).shares()) == HOURS


def test_shares_split_the_day():
    weekday = DailyProfile()
    shares = weekday.shares()
    assert len(shares) == HOURS and abs(sum(shares) - 1) < 1e-12
    assert shares[8] > max(shares[5], shares[11]) and shares[16] > max(shares[12], shares[20])  # two peaks
    assert shares[3] < shares[8] / 10  # quiet night
    flat = DailyProfile("points", [(0, 0.5), (24, 0.5)])
    assert all(abs(share - 1 / HOURS) < 1e-12 for share in flat.shares())
    assert DailyProfile("poly", [], [-1.0]).shares() == [1 / HOURS] * HOURS  # zero all day: even


def test_a_peak_hour_factor_shapes_the_hours_quarters():
    from traffic_simulator.daily_profile import PEAK_QUARTER, phf_of, phf_quarters, quarter_shares
    quarters = phf_quarters(0.8)
    assert abs(sum(quarters) - 1) < 1e-12 and abs(phf_of(quarters) - 0.8) < 1e-12  # PHF = V / (4·V15)
    assert quarters[PEAK_QUARTER] == max(quarters) and len(set(round(q, 12) for q in quarters)) == 2  # the rest even
    assert phf_quarters(1) == () and phf_of(()) == 1 and phf_quarters(0.25)[PEAK_QUARTER] == 1  # even; all at once
    assert quarter_shares([1000, 1200, 1100, 1000]) == (1000 / 4300, 1200 / 4300, 1100 / 4300, 1000 / 4300)
    assert abs(phf_of(quarter_shares([1000, 1200, 1100, 1000])) - 4300 / 4800) < 1e-12  # HCM 2000's example
    for bad in ([1, 2, 3], [1, -1, 1, 1], [0, 0, 0, 0], ["a"] * 4, None):
        try:
            quarter_shares(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    for bad in (0.2, 1.1):
        try:
            phf_quarters(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_polynomials_fit_and_convert():
    exact = [0.2, -1.0, 3.0, -1.5]  # a cubic
    samples = [(i / 50, sum(a * (i / 50) ** k for k, a in enumerate(exact))) for i in range(51)]
    assert all(abs(a - b) < 1e-7 for a, b in zip(fit_polynomial(samples, 3), exact))
    cubic = DailyProfile("poly", [], exact)
    quartic = cubic.as_polynomial(4)  # a higher degree keeps the same curve
    assert all(abs(cubic.level(h / 4) - quartic.level(h / 4)) < 1e-6 for h in range(97))
    points = DailyProfile().as_polynomial(6).as_points()
    assert points.kind == "points" and len(points.points) == 9 and points.points[-1][0] == HOURS
    peak = DailyProfile("poly", [], [0.0, 4.0, -4.0])  # 4t(1 - t): midday peak, zero at the ends
    assert abs(peak.level(12) - 1.0) < 1e-12 and peak.level(0) == 0.0 and peak.level(24) == 0.0
    assert DailyProfile("poly", [], [0.5, -2.0]).level(24) == 0.0  # negative parts count as zero


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
