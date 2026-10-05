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


def test_shares_split_the_day():
    weekday = DailyProfile()
    shares = weekday.shares()
    assert len(shares) == HOURS and abs(sum(shares) - 1) < 1e-12
    assert shares[8] > max(shares[5], shares[11]) and shares[16] > max(shares[12], shares[20])  # two peaks
    assert shares[3] < shares[8] / 10  # quiet night
    flat = DailyProfile("points", [(0, 0.5), (24, 0.5)])
    assert all(abs(share - 1 / HOURS) < 1e-12 for share in flat.shares())
    assert DailyProfile("poly", [], [-1.0]).shares() == [1 / HOURS] * HOURS  # zero all day: even


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
