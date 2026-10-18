"""The agent tools' statistics (stats.py): distributions against table values, paired comparison,
replications, Poisson dispersion, calibration scores, logit and reliability.
Run with `python tests/test_stats.py` (or the test monitor)."""

import random

from traffic_simulator import stats
from traffic_simulator.simulation import arrivals


def test_t_and_chi_square_match_their_tables():
    for confidence, df, table in ((0.95, 2, 4.303), (0.95, 30, 2.042), (0.99, 10, 3.169), (0.95, 1, 12.706)):
        assert abs(stats.t_quantile(confidence, df) - table) < 1e-3, (confidence, df)
    assert abs(stats.t_p(2.776, 4) - 0.05) < 1e-3
    for x, k in ((3.841, 1), (18.307, 10), (124.342, 100)):  # each χ² table's 5 % point
        assert abs(stats.chi2_sf(x, k) - 0.05) < 1e-3, (x, k)
    assert stats.percentile([1, 2, 3, 4, 5], 50) == 3 and stats.percentile([7], 95) == 7


def test_paired_seeds_and_replications():
    base, other = {1: 10.0, 2: 20.0, 3: 30.0}, {1: 11.0, 2: 21.2, 3: 30.9}
    found = stats.paired(base, other)
    assert abs(found["diff"] - 1.033) < 1e-3 and found["clear"] and found["p"] < 0.05  # common seeds: clear
    assert stats.paired(base, {1: 11.0})["clear"] is None  # one shared seed can't show it
    assert stats.paired(base, {k: v + 2 for k, v in base.items()})["clear"]  # the same shift every seed
    assert stats.replications([100, 100, 100]) == 1
    assert stats.replications([90, 100, 110]) > 20  # 10 % SD needs many runs for ±5 %
    assert stats.replications([90, 100, 110], precision=0.5) < stats.replications([90, 100, 110])


def test_dispersion_tells_the_arrival_patterns_apart():
    rng = random.Random(3)
    verdicts = {pattern: stats.dispersion([arrivals(rng, pattern, 8) for _ in range(300)])
                for pattern in ("Random", "Poisson", "Binomial", "Negative binomial")}
    assert verdicts["Poisson"]["verdict"].startswith("consistent with Poisson")
    assert verdicts["Random"]["verdict"] == verdicts["Binomial"]["verdict"] == "more regular than Poisson"
    assert verdicts["Negative binomial"]["verdict"].startswith("bunched") and verdicts["Negative binomial"]["index"] > 2
    assert stats.dispersion([1, 2])["verdict"] == "too few counts to test"


def test_calibration_scores_logit_and_reliability():
    scores = stats.fit([(500, 500), (420, 400), (900, 1200)])
    assert scores["geh_under_5_percent"] == 66.7 and not scores["meets_geh_target"] and scores["slope"] > 1  # model low
    assert stats.fit([(100, 100)] * 3)["rmse_vph"] == 0 and round(stats.geh(400, 600), 2) == 8.94
    shares = stats.logit({"Car": -0.5, "Bus": -1.5})
    assert abs(sum(shares.values()) - 1) < 1e-12 and abs(shares["Car"] - 1 / (1 + 2.718281828 ** -1)) < 1e-6
    assert stats.reliability([(100, 0)] * 10) == {"tti": 1.0, "pti": 1.0, "bi": 0.0}  # no delay: free flow
    assert stats.reliability([(200, 100)] * 19 + [(400, 300)])["pti"] > stats.reliability([(200, 100)] * 20)["pti"]
    assert stats.reliability([]) == {"tti": None, "pti": None, "bi": None}
    mixed = stats.reliability([(1000, 0), (20, 10)])  # a long free trip, a short slow one: one mean for TTI and BI
    assert mixed["tti"] == 1.5 and mixed["bi"] == round((mixed["pti"] - 1.5) / 1.5, 3)
    # Buffer index, mean-based: nine free trips and one taking 3× free; ratios' 95th percentile 2.1, mean 1.2
    assert stats.reliability([(100, 0)] * 9 + [(300, 200)]) == {"tti": 1.2, "pti": 2.1, "bi": 0.75}


def test_anova_signed_rank_and_regressions():
    assert abs(stats.f_sf(4.26, 2, 9) - 0.05) < 1e-3  # the F table's 5 % point
    blocked = stats.anova({"A": {1: 10, 2: 20, 3: 30}, "B": {1: 9, 2: 19, 3: 29.5}, "C": {1: 11, 2: 21, 3: 30}})
    assert blocked["design"].startswith("randomized block") and blocked["p"] < 0.05  # seeds as blocks see it
    assert stats.anova({"A": {1: 10, 2: 20, 3: 30}, "B": {4: 9, 5: 19, 6: 29.5}})["design"] == "one-way"
    assert stats.signed_rank([1, 2, 3, 4, 5]) == 0.0625 and stats.signed_rank([0, 0]) is None  # 2/32, exact
    fit = stats.ols([[1], [2], [3], [4], [5]], [2.1, 3.9, 6.2, 7.8, 10.1], ["x"])
    assert abs(fit["coefficients"][1]["coefficient"] - 1.99) < 1e-9 and fit["r2"] > 0.99 and fit["f_p"] < 0.001
    rng = random.Random(1)

    def poisson(mean):
        limit, k, p = 2.718281828459045 ** -mean, 0, 1.0
        while True:
            p *= rng.random()
            if p < limit:
                return k
            k += 1

    xs = [[rng.uniform(0, 3)] for _ in range(400)]
    counts = [poisson(2.718281828459045 ** (0.5 + 0.8 * x[0])) for x in xs]
    found = stats.poisson_regression(xs, counts)
    assert abs(found["coefficients"][1] - 0.8) < 0.1 and 0.8 < found["dispersion"] < 1.25
    bunched = [poisson(2.718281828459045 ** (0.5 + 0.8 * x[0]) * rng.gammavariate(2, 0.5)) for x in xs]
    assert stats.poisson_regression(xs, bunched)["dispersion"] > 2  # gamma-mixed: overdispersed
    assert stats.empirical_bayes(10, 2, 0) == 2 and abs(stats.empirical_bayes(10, 2, 1e9) - 10) < 1e-6


def test_a_poisson_safety_fit_ranks_sites_by_their_departure():
    from traffic_simulator import experiments

    counts, flows = [1, 2, 2, 3, 3, 6], [300, 400, 500, 600, 700, 800]  # not overdispersed: EB = μ, no excess
    roads = [{"road": f"r{i}", "flow_vph": f, "vehicle_km": 10.0, "delay_veh_h": 0.0} for i, f in enumerate(flows)]
    observed = {"conflicts": [{"road": f"r{i}", "link": f"r{i}>", "t": 1.0, "type": "Car", "leader_type": "Car",
                               "speed_kmh": 30.0, "ttc_min": 1.0, "drac_max": 1.0} for i, c in enumerate(counts) for _ in range(c)],
                "links": {f"r{i}>": {"road": f"r{i}", "length_m": 500.0} for i in range(6)}}
    found = experiments.safety({"roads": roads, "observed": observed})
    assert found["spf"]["model"] == "Poisson" and found["ranked_by"].startswith("z")
    z = [site["z"] for site in found["ranked"]]
    assert z == sorted(z, reverse=True) and found["ranked"][0]["road"] == "r5"  # 6 against 5.0 expected


def test_lopsided_counts_and_failed_analyses_never_stop_a_workbook():
    from traffic_simulator import experiments

    try:  # the fit runs a site's mean towards 0: never a division by zero, at worst an honest "can't fit"
        assert all(m > 0 for m in stats.poisson_regression([[1.0], [2.0], [3.0], [4.0], [5.0]], [0, 0, 0, 0, 40])["mu"])
    except ValueError:
        pass
    roads = [{"road": f"r{i}", "flow_vph": 600, "vehicle_km": 10.0, "delay_veh_h": 0.0} for i in range(5)]
    observed = {"conflicts": [{"road": "r0", "link": "r0>", "t": 1.0, "type": "Car", "leader_type": "Car",
                               "speed_kmh": 30.0, "ttc_min": 1.0, "drac_max": 1.0}],
                "links": {f"r{i}>": {"road": f"r{i}", "length_m": 500.0} for i in range(5)}}
    ranked = experiments.safety({"roads": roads, "observed": observed})  # every flow alike: no model, a ranking
    assert ranked["ranked"][0]["road"] == "r0" and "ranked by rate" in ranked["note"]
    sheets = experiments.analysis_sheets({"observed": {"trips": "not a list of trips"}, "roads": []})
    assert sheets[-1][0] == "Analysis note"  # the analysis failed; the run's workbook still gets written


def test_clusters_envelopes_forecasts_and_steady_state():
    rng = random.Random(2)
    points = [[rng.gauss(cx, 0.1), rng.gauss(cy, 0.1)] for cx, cy in ((0, 0), (1, 1), (0, 1)) for _ in range(20)]
    assert stats.choose_k(points) == 3 and len(set(stats.kmeans(points, 3)[0])) == 3
    days = [[100 + 5 * t + rng.gauss(0, 5) for t in range(12)] for _ in range(8)]
    rep = stats.representative_day(days)
    good = stats.envelopes(days, days[rep], "flow")
    assert good["criteria"]["III"] and good["criteria"]["IV"] and good["inside_1_sigma"] == 12  # itself: inside
    assert not any(stats.envelopes(days, [v + 60 for v in days[rep]], "flow")["criteria"].values())
    try:
        stats.envelopes(days[:2], days[0], "flow")
    except ValueError:
        pass
    else:
        raise AssertionError("two days can't make an envelope")
    trend = stats.holt([10 + i + rng.gauss(0, 0.3) for i in range(30)], 3)
    assert 38 < trend["forecast"][0]["value"] < 42 and trend["forecast"][2]["high"] > trend["forecast"][0]["high"]
    assert stats.mser([60, 50, 40, 30, 20] + [10] * 40) == 5  # the ramp is the warm-up
    steady = stats.batch_means([10 + rng.gauss(0, 1) for _ in range(60)])
    assert abs(steady["mean"] - 10) < 1 and steady["batches"] == 10
    assert stats.gini([1, 1, 1, 1]) == 0 and stats.gini([0, 0, 0, 10]) == 0.75
    probabilities, _ = stats.nested_logit({"Car": -1, "Bus": -1.5}, {})
    assert all(abs(probabilities[k] - v) < 1e-12 for k, v in stats.logit({"Car": -1, "Bus": -1.5}).items())
    nested, _ = stats.nested_logit({"Car": -1, "Bus": -1.5, "Tram": -1.5}, {"public": (["Bus", "Tram"], 0.3)})
    flat = stats.logit({"Car": -1, "Bus": -1.5, "Tram": -1.5})
    assert nested["Car"] > flat["Car"]  # a near-copy alternative takes share from its twin, not from the car (IIA)


def test_rank_tests_and_all_pairs_match_known_values():
    ordered = {"A": {1: 10, 2: 11, 3: 12, 4: 13}, "B": {1: 12, 2: 13, 3: 14, 4: 15}, "C": {1: 20, 2: 19, 3: 22, 4: 21}}
    found = stats.friedman(ordered)  # every seed ranks A < B < C: Q = 8 on 2 df, p = e^-4
    assert found["q"] == 8.0 and abs(found["p"] - 0.0183156) < 1e-6 and found["mean_ranks"] == {"A": 1.0, "B": 2.0, "C": 3.0}
    tied = stats.friedman({"A": {1: 5, 2: 5}, "B": {1: 5, 2: 5}})
    assert tied["p"] == 1.0  # all tied: no difference
    assert stats.friedman({"A": {1: 1}, "B": {1: 2}}) is None  # one seed: nothing to rank on
    separated = stats.mann_whitney([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
    assert separated["u"] == 0 and abs(separated["z"] - 2.507) < 1e-3  # (12.5 - 0.5) / sqrt(25·11/12)
    same = stats.kruskal_wallis({"a": [1, 2, 3], "b": [1, 2, 3]})
    assert same["h"] == 0.0 and same["p"] == 1.0
    assert stats.kruskal_wallis({"a": [1, 2, 3, 4], "b": [5, 6, 7, 8], "c": [9, 10, 11, 12]})["p"] < 0.05
    ranked = stats.all_pairs(ordered, higher_is_better=False)
    assert ranked["comparisons"] == 3 and [r["scenario"] for r in ranked["ranked"]] == ["A", "B", "C"]
    assert [r["group"] for r in ranked["ranked"]] == ["a", "b", "c"]  # every pair told apart
    rng = random.Random(3)
    noisy = {name: {seed: rng.gauss(0, 1) for seed in range(6)} for name in "XYZ"}  # Y and Z differ, X sits between
    assert [(r["scenario"], r["group"]) for r in stats.all_pairs(noisy)["ranked"]] == [("Z", "a"), ("X", "ab"), ("Y", "b")]


def test_two_way_anova_with_interaction():
    additive = {("a1", "b1"): [10, 11, 12], ("a1", "b2"): [20, 21, 22], ("a2", "b1"): [15, 16, 17], ("a2", "b2"): [25, 26, 27]}
    found = stats.two_way_anova(additive)  # A adds 5, B adds 10, no interaction; within-cell variance 1
    assert found["A"]["f"] == 75.0 and found["B"]["f"] == 300.0 and found["interaction"]["f"] == 0.0
    assert found["A"]["df"] == [1, 8] and found["interaction"]["p"] == 1.0 and found["replicates"] == 3
    crossed = dict(additive)
    crossed[("a2", "b2")] = [35, 36, 37]  # B's effect doubles at a2: an interaction
    assert stats.two_way_anova(crossed)["interaction"]["p"] < 0.001
    single = stats.two_way_anova({k: v[:1] for k, v in additive.items()})
    assert single["interaction"] is None and single["replicates"] == 1  # no replicates: the interaction is the error
    assert stats.two_way_anova({("a1", "b1"): [1], ("a2", "b1"): [2]}) is None  # B needs two levels


def test_power_of_a_paired_comparison():
    # effect size 1 (difference = SD of differences): 10 pairs give 80 % power at 5 %, the textbook figure
    assert abs(stats.paired_power(1.0, 1.0, 10) - 0.80) < 0.02
    assert stats.seeds_for_power(1.0, 1.0, 0.8) == 10 and stats.seeds_for_power(1.0, 1.0, 0.9) > 10
    assert stats.paired_power(1.0, 0.5, 30) > stats.paired_power(1.0, 0.5, 10)  # more seeds, more power
    smallest = stats.detectable(1.0, 10, 0.8)
    assert abs(stats.paired_power(1.0, smallest, 10) - 0.8) < 0.03  # the detectable difference has that power
    assert stats.seeds_for_power(1.0, 0.0) is None and stats.paired_power(0.0, 1.0, 3) == 1.0
    # exact noncentral t (G*Power): few seeds no longer understated (the shifted central t gave 0.31 at n 5)
    assert abs(stats.paired_power(1.0, 1.0, 5) - 0.401) < 0.002 and abs(stats.paired_power(1.0, 0.5, 20) - 0.565) < 0.002
    assert abs(stats.paired_power(1.0, 0.0, 8) - 0.05) < 1e-3  # no difference: found at the test's α


def test_holm_adjusts_p_values_tested_together():
    assert stats.holm([0.01, 0.04, None, 0.03, 0.2]) == [0.04, 0.09, None, 0.09, 0.2]
    assert stats.holm([0.5]) == [0.5] and stats.holm([]) == [] and stats.holm([0.3, 0.4]) == [0.6, 0.6]


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
