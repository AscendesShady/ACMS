# Statistics in transportation engineering: which method is used where

A catalogue of the statistical methods in `references/transportation_engineering_books/` (SRC_001 to SRC_033), each with the
transport problem the books apply it to, its underlying distribution or assumption, and the simulator
tool it could become for agentic control of ACMS. Page numbers are **PDF pages** (not printed pages) and are
written `020 p681` (book SRC_020, PDF page 681). Every pairing was read in the book's text. Where a book
only cites someone else's study, the row says "(lit.)". "In ACMS" says what the code already does.

## 1. The books

| Group | Books |
|---|---|
| **Statistics taught directly** (methods first, examples second) | 033 McClave & Sincich, *Statistics* 13th ed. (descriptive to multiple regression, ANOVA, χ²); 032 Snell, *Applied Statistics: a handbook of BMDP analyses* (the companion to Cox & Snell: worked examples of ANOVA, logistic, log-linear, gamma/exponential fitting, survival) |
| **Statistics written for transport** | 020 Washington, Karlaftis & Mannering, *Statistical and Econometric Methods for Transportation Data Analysis*: every chapter is a method with a transport example |
| **Statistics applied, heavily** | 009 Handbook of Transportation Science (choice, queueing, traffic flow, assignment); 011 HCM 2000 (delay, queues, gap acceptance, percentiles); 027 FHWA Toolbox Vol III Microsimulation 2019 (replications, calibration, clustering); 031 FHWA Traffic Simulation (random numbers, headway generation, output analysis); 001 Bus Transport (logit family, elasticities, dwell); 013 ML for Transportation (RL signal control, forecasting, clustering); 007 Explainable AI for ITS (SARIMA, imputation, GA, CHAID); 014 ML in Transportation (tests, ANOVA, clustering) |
| **Statistics applied, lightly** | 022 Green Book (percentile speeds, storage overflow probability); 029 Vulnerability Analysis (logit link use, Bayesian incident detection, risk); 008 Mobility to Accessibility (gravity accessibility, percentiles, Gini); 024 Safer Roads (blackspot ranking, before/after); 030 Geography of Transport (gravity, graph indices, Gini); 028 Transport Policy (service-quality survey models, lit.); 012 ITS (lit. pointers); 004 Computational Learning and IoT (fuzzy signal controller, input-output queues); 003 AIoT (survey); 023 Urban Transportation Problem (1965 regressions) |
| **Essentially none** | 002 Intermodal Freight ("hazard" = hazardous goods); 005 Multimodal Future; 006 Computing for Transportation (engine/sensor faults only); 010 FHWA HPMS capacity (deterministic HCM formulas); 015 Mobility and Transportation (scenario runs, no statistics); 016 Fuzzy Transportation Problems (OR shipping LP, no road traffic); 017 Private Financing ("Markov" is an author, "probit" is "probity"); 018 Road Transport Act 2018 (law); 019 Soviet Transportation Policy (trend lines, Lorenz curves); 021 Stochastic Optimal Transportation (pure mathematics, no traffic); 025, 026 FHWA Toolbox Vol I, II (tool selection) |

Caveats: 033's nonparametrics chapter is online-only (only its tables, p858–860, are in the PDF). 027 is the
2019 edition, which replaced GEH/Theil's U with variation envelopes and Criteria I–IV. In 029 "Weibull"
means J.W. Weibull's accessibility axioms and "hazard" means natural hazard, not survival models.

## 2. The catalogue

### 2.1 Descriptive and traffic-stream statistics

| Method | Distribution / assumption | Used for | Sources | Simulator tool (sim → output) | In ACMS |
|---|---|---|---|---|---|
| Mean, median, SD, CV, skewness, kurtosis | none; shape relative to normal | Spot-speed summaries, urban vs rural speed shape | 020 p15–34; 033 §2.3–2.4 p84–103; 014 p27–34 | Detector speeds/headways → summary per lane | Mean speed per road (results) |
| Percentiles: 85th-percentile speed | empirical speed CDF | Operating speed, design speed, passing-zone length; free-flow speed ≈ 85th − 3 km/h | 022 p119–121, p212, p342; 011 p52, p231, p785; 024 p60; 020 p27 | Free-flow speeds → v85 per link | Yes: `read_detectors` |
| Percentiles of driver traits | empirical | Reaction time (2.5 s > 90th pct), deceleration 3.4 m/s² (90 % brake harder), 15th-pct walking speed 1.2 m/s | 022 p102–107, p201–202; 011 p354 | Bounds for the driver-parameter draws | No |
| Space-mean (harmonic) vs time-mean speed; Edie's q, k, v | stationary traffic | Detector speed measurement, flow = 1/mean headway, density = 1/mean spacing | 009 p162–168; 011 p85–86 | Spot speeds / trajectories → SMS, q, k | Yes: `read_detectors` |
| Occupancy → density | effective length uncorrelated with pace | Single-loop speed and density | 009 p168–170 | Detector occupancy → k, v | Yes: `read_detectors` |
| Saturation headway, lost time from queued discharge | sample mean after 4th vehicle, ≥ 15 cycles | Field saturation flow | 011 p90–91, p124–125, p508; 009 p180–183 | Stop-line passages → s, lost time, s·g/C | Yes: measured (`read_detectors`) |
| % headways < 3 s | proportion | Percent time-spent-following (two-lane LOS) | 011 p236 | Detector headways → PTSF | % < 3 s (`read_detectors`) |
| Headway CV; E(W) = E(h)(1 + CV²)/2 | random passenger arrival | Bus bunching, transit headway adherence LOS (CV ≤ 0.10 = A), passenger wait | 009 p127, p155; 011 p1012–1013; 013 p127; 023 p195 | Bus headways at a stop → CV, expected wait | Yes: `analyse_run` |
| Ranked hourly volumes, K-factor, PHF | empirical | Design hour (30th highest hour), peaking | 011 p104–107; 022 p110–114 | Daily profile → design-hour demand | Daily curve exists |
| Planning time index (95th-pct TT / free-flow TT), TTI, SD of travel time | empirical percentile | Travel-time reliability | 027 p39, p92–93; 029 p47 | Trips over seeds → PTI per OD | Yes: TTI, PTI and the buffer index (mean-based) per run and OD (`analyse_run`), compared in experiments |
| Gini / Lorenz curve | inequality | Concentration of traffic on links, equity of transit access | 030 p646–649; 019 p111; 008 p107 | Link flows → Gini | Yes: `analyse_run` |
| Box plot fences, z-score outliers, count-consistency (> 10 % up/downstream) | normal for z | Detector data QC, bad runs | 014 p66–67, p88–93; 033 §2.7; 027 p40, p78 | Detector series / runs → flagged outliers | No |
| Exposure-normalised crash rates | harm / exposure | Deaths per veh-km; blackspot ranking (per km, per million entering, severity weights 12/3/1) | 009 p73–82; 024 p148–149; 022 p142–144 | Conflicts and veh-km per junction → ranked list | Veh-km recorded |
| Graph indices (α, β, γ, detour, Shimbel) | none | Network structure, accessibility | 030 p613–639 | Built network → indices | Yes: `network_indices` |

### 2.2 Probability distributions

| Distribution | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|
| **Poisson** (counts at a constant rate) | Vehicle arrivals per interval or cycle, crashes per month; P(0 arrivals) for phase skipping | 033 §4.5 p244–257; 020 p930 (arrivals at a queue, accidents ≈ Poisson); 011 p460; 032 Ex A p19 | Generate arrivals; test that counts are Poisson (variance = mean) | Yes: `simulation.arrivals`; dispersion test (`analyse_run`) |
| **Binomial / negative binomial** | Regular vs bunched arrivals; turn counts; NB for overdispersed crashes | 033 §4.4; 020 p654–662 | Arrivals; dispersion check | Yes (NB k = 2) |
| **Exponential / shifted (negative) exponential** | Headways of random flow, with a minimum headway (no headways under 1 s); time between events | 031 p12–13 (h = (H − h_min)(−ln(1 − R)) + h_min); 011 p124; 033 §5.6 p297–301 (bus inter-arrivals); 009 p127 | Headway generator; P(gap > t) for gap acceptance | Arrivals per minute, not headways |
| **Bunched exponential (M3)** | Free proportion φ = e^(−bq), minimum headway Δ: actuated green extension | 011 p436, p456–457 | Fit to sim headways | No |
| **Shifted log-normal** | Manual-driver time headways (mean ≈ 1.8 s) | 009 p235 | Check the sim's headway histogram | No |
| **Normal** | Freeway speeds; bicycle speeds (18 ± 3 km/h); dwell times (one-tail Z_a for stop failure rate); Synchro's percentile volumes λ + z√λ | 020 p930; 011 p684–687, p1016–1018; 014 p29–35 | P(speed > limit), stop capacity | Yes: Synchro percentile delay |
| **Uniform** | Driver aggressiveness 1–10; random arrival within a cycle; toll-booth service times | 031 p13, p15–16; 011 p1146–1154; 033 p266–269; 004 p150 | Driver-type draws | Seeded `random` |
| **Gamma, Weibull, log-logistic** | Durations (see 2.8); Gamma heterogeneity turns exponential deterrence into a power law | 032 Ex T p125–130; 020 p590–625; 009 p59–61, p671 | Duration fitting | No |
| **Gumbel (EV type I)** | Utility errors → logit | 009 p20; 020 p687 | See 2.7 | No |
| **Multivariate normal** | Utility errors → probit; probit SUE link-time perception | 009 p26–27, p380; 020 p679 | See 2.7 | No |
| **Power law** | Network degree distribution | 029 p340 | Descriptive only | No |

### 2.3 Estimation, sample size and simulation output analysis

| Method | Assumption | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|---|
| t confidence interval for a mean | normal sampling (CLT) | Mean speed, delay, travel time across replications | 033 §7.3 p357; 020 p62–66; 027 p90–91; 031 p28–29 | KPI over seeds → mean ± half-width | Yes: `experiments.summary` |
| **Required replications** N = (t·s / (μ·e))² from 4 seed runs; N > 20 means instability or gridlock | t, n − 1 df | Runs per scenario | 027 p89–91, p93; 033 §7.5 p374 | Pilot seeds → runs needed, instability flag | Yes: `replications_needed`, `unstable` (`run_experiment`) |
| Sample size from index of dispersion: N = I/ε², I = Var(n)/E(n) | IID counts | Vehicles to observe for a capacity / saturation-flow estimate | 009 p176–179, p182 | Counts → how long to run before trusting a capacity | No |
| Sample size n = 4s²/ε² | normal / t | Field observations for local defaults | 011 p142–143 | Measurements needed | No |
| CI for a proportion, CI for a variance (χ²) | normal approx. / χ² | Mode or turn share; speed variance | 020 p67–74; 033 §7.4, §7.6 | Shares and variances → CIs | No |
| Batch means; warm-up; independent random-number streams | output autocorrelated, non-stationary | One long run split into batches | 031 p25–29; 011 p1160–1163 | Long run → CI without many seeds | Yes: MSER-5, batch means (`analyse_run`) |
| Linear congruential generator, seed control | uniform(0,1) | Reproducible stochastic simulation | 031 p12 | — | Yes: seeded `random.Random` |
| Maximum likelihood, simulated ML (Halton draws) | — | Estimating choice, count and duration models | 009 p31–32; 020 throughout; 001 p151–183 | Calibrate parameters to sim or field data | No |

### 2.4 Comparing alternatives (tests, ANOVA, nonparametrics)

| Method | Assumption | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|---|
| Two-sample t (pooled), **Welch** | normal; equal / unequal variance | Before/after speed limit repeal; alternative 1 vs 2 on delay, TT, PTI | 020 p93–104; 027 p91–92, p94; 033 §9.2 p465–476 | Plan A vs B over seeds | Yes: `experiments.difference` |
| **Paired t** (common random numbers) | normal differences | Red-light cameras before/after at 13 junctions | 033 §9.3 p483–495; 020 p108 | Same seeds under two controllers → paired difference (tighter than Welch) | Yes: `run_experiment` (`stats.paired`) |
| Two proportions z | normal approx. | Sign failure rates, stop rates | 033 §9.4 p495–502; 020 p109 | Share stopped / spillback cycles between plans | No |
| F test of variances; χ² test of one variance | normal | Did a change alter speed variability? | 020 p86, p118–124; 033 §8.8, §9.6 | Speed or headway variance between plans | No |
| One-way ANOVA + Tukey/Bonferroni; randomized block; factorial | normal, equal variance | ≥ 3 groups (fatigued driving, SMA additives); plan × demand scenario | 033 §10.2–10.5 p541–599; 014 p35–41, p105–107; 011 p142 | Rank ≥ 3 signal plans, demand as block | Yes: randomized-block ANOVA, Bonferroni |
| Mann-Whitney, Wilcoxon signed-rank, Kruskal-Wallis, sign test | ranks, no normality | Speeds at two locations, preference counts | 020 p125–160 | Non-normal delay distributions between plans | Wilcoxon signed-rank |
| Before/after with control group; regression to the mean | — | Countermeasure evaluation, speed-limit and belt-law effects | 024 p152; 009 p87, p93–95 | Scenario study design | Baseline vs variants |

### 2.5 Calibration, validation and goodness of fit

| Method | Assumption | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|---|
| **χ² goodness of fit**, contingency independence | expected counts ≥ 5 | Accidents by type; testing normality; headways vs Poisson | 020 p160–165; 033 §13.1–13.3 p803–833 | Binned headways/counts vs Poisson/exponential; turn share vs time | No |
| Kolmogorov-Smirnov, Q-Q / normal probability plots | — | Normality of residuals or speeds | 020 p274–275, p320; 033 §5.4 p283 | Distribution check of sim outputs | No |
| Gamma likelihood-ratio test of index = 1 (i.e. exponential) | gamma ML | Are failure intervals random? | 032 Ex T p125–130 | Are sim headways random? | No |
| **Variation envelopes ±1σ / ±1.96σ, Calibration Criteria I–IV** (BDAE, systematic error) | day-to-day variation | Pass/fail calibration of 15-min travel time and throughput | 027 p75–80 | Sim vs observed profiles → pass/fail | Yes: `check_calibration` profiles |
| Representative day (min squared % deviation from cluster mean) | — | Choosing the calibration target | 027 p73–74, p81 | — | No |
| Field-vs-model regression (slope = accuracy, R² = precision) | linear | Validating capacity/delay models | 011 p135 | Sim vs detector values | Yes: slope, R² (`check_calibration`) |
| RMSE, MAE, MAPE, MPE, MaxAPE, AIC/BIC, Ljung-Box | — | Forecast and imputation accuracy (MAPE preferred for transport) | 020 p490–493; 007 p91; 013 p87, p170 | Score forecasts and calibration | Calibration: RMSE, %RMSE, MAPE (`check_calibration`) |
| Count-to-OD calibration | — | Demand matching observed counts | 011 p1087–1091 (T_ij = T_i·T_j/ΣT_j); 027 p40 | Counts → OD seed | Yes: calibrate demand to counts |

### 2.6 Regression

| Method | Assumption | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|---|
| OLS / multiple regression with t, F, R² | normal homoscedastic errors | AADT ~ population, lanes (020 Ex 3.1); speed ~ link features; crash SPF ~ length, AADT (033 p704); rut depth; cost/km ~ service quality (001) | 020 p167–305; 033 Ch 11–12; 013 p41–45; 001 p115–116 | Metamodel: delay ~ v/c, green ratio across runs | Yes: `regress` |
| OLS imputation from neighbouring detectors | normal errors | Missing lane flow at detector #138 from 3 neighbours | 007 p137–140 | Virtual detector | No |
| Logistic (binary) regression | Bernoulli, logit link | Congested or not ~ speed, volume (013 p46); propensity to speed (020 p309); fatality risk (012 p75) | 013 p46–47; 020 p309; 032 Ex H, L, M, V, X | P(congested), P(red-run) ~ sim state | No |
| Weighted LS after heteroscedasticity tests (Breusch-Pagan, White) | variance ~ covariates | Transit subsidy vs performance | 020 p333–368 | Delay variance grows with v/c | No |
| Durbin-Watson, AR(1) corrections | AR errors | Monthly bus-card demand (Athens) | 020 p368–377 | Regressions over consecutive intervals | No |
| Simultaneous equations (2SLS/3SLS), SURE | endogenous regressors | **Lane mean speeds depending on adjacent lanes** on a 3-lane freeway; household travel times | 020 p385–402 | Lane detector speeds/flows → interlane system (lane-change calibration) | No |
| Panel fixed / random effects | error components | State fatalities × years; BRT ridership (fare elasticity −0.366, headway −0.243) | 020 p403–440; 001 p196–205 | Junction × interval panels; headway → ridership | No |
| Tobit / truncated; Heckman selectivity | censored normal | Outcomes censored at 0 or capacity; speed only on chosen route | 020 p314–315, p791–823 | Queue censored at storage | No |
| Box-Cox, BPR / volume-delay curves | power transform | Functional form; link travel time ~ v/c | 020 p316, p1066; 011 p1105–1137 | Fit volume-delay to sim links | No |
| Speed–risk power law (crash ∝ v, injury ∝ v², fatal ∝ v⁴) | empirical | Safety effect of speed-limit change | 009 p92–93 | Δ mean speed → Δ crashes | Yes: experiments (`speed_risk`) |
| Dwell-time regression (dwell ∝ standees² × boardings + alightings) | (lit.) | Bus dwell under crowding | 001 p295, p306 | Calibrate stop dwell | Fixed dwell |
| Meta-regression of elasticities | — | Fare (≈ −0.4), IVT, headway elasticities; crowding multipliers | 001 p258–269 | Default demand elasticities | No |
| Elasticities (point, arc) | — | Response of Y to X | 020 p317; 008 p71–75; 011 p1056 | Δ demand → Δ delay | No |

### 2.7 Discrete choice (random utility)

| Method | Distribution | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|---|
| **Multinomial logit** | IID Gumbel; IIA | Mode choice, route choice (151 commuters: arterial/two-lane/freeway), service-quality index, HCM default U = −0.025·IVT − 0.050·OVT − 0.005·Cost | 020 p687–764; 009 p20–22, p675; 001 p107–134, p300–306; 011 p1057–1058 | OD times/costs → mode or route shares | Mode: `choose_modes` (HCM default utility); an institution's split with Walk, constants calibrated to observed shares (`institution_mode_split`) |
| **Binary / multinomial probit** | MVN errors, correlated alternatives | Mode choice; probit route choice / stochastic network loading | 020 p679–687; 009 p26–27, p37, p57 | Link TT variance → probit route flows (Monte Carlo) | No (path-size logit instead) |
| Nested, cross-nested, paired combinatorial logit (GEV) | correlation within nests | Routes sharing sections; public vs private mode nests; adjacent departure slots | 009 p22–26, p54–55; 020 p765–769; 029 p181 | Overlap-aware route/mode split; logsum accessibility | Nested logit, logsum (`choose_modes`) |
| Path Size / C-logit | MNL + overlap term | Route choice on overlapping paths | 009 p35–36 | Path lengths → PS terms | Yes: route choice |
| Mixed logit, latent class | random tastes (normal, log-normal, triangular) | Heterogeneous value of time; tender and BRT/LRT preferences | 009 p29–31, p57–58; 020 p772; 001 p172–248 | VOT classes → shares | No |
| Ordered logit / probit | logistic / normal latent | Crash severity (PDO/injury/fatal); HOV opinion; BRT rating | 020 p777–790; 001 p206–213 | Conflict severity by TTC/speed band | No |
| Departure-time choice with schedule delay | logit/probit; latent preferred arrival | Peak spreading; the book says TT(DT) can come from a traffic simulation | 009 p37–40 | Sim TT by slot → time-of-day profile | No |
| IIA tests (Hausman-McFadden), ρ², LR tests | asymptotic χ² | MNL vs nested logit choice | 020 p725–757 | — | No |
| Sample enumeration (never plug in mean X) | logit non-linearity | Aggregate forecast: 0.218/0.684/0.098 vs biased 0.152/0.794/0.053 | 020 p760–764 | Average individual probabilities | No |
| Logsum welfare, value of time, elasticities | Gumbel | Benefit of a change; VOT = time/cost coefficients | 020 p769–772; 009 p677–680; 029 p181 | Δ sim times → money benefit | Logsum, value of time (`choose_modes`) |

### 2.8 Duration (hazard) models

| Method | Distribution | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|---|
| Cox proportional hazards | semiparametric | Commuters delaying the work-to-home trip ~ ratio of actual / free-flow TT | 020 p583–589; 032 Set 3 | Congestion ratio → departure shift | No |
| Parametric exponential / Weibull / log-logistic + gamma heterogeneity | named; LR test (Weibull p = 1.745 beats exponential) | Same commuter delay; activity, home-stay, border-crossing durations | 020 p590–625; 009 p59–61 | Fit queue-clearance, signal-wait, incident durations | No |
| Kaplan-Meier, life tables, right censoring | independent censoring | Accident-free periods | 020 p575–583; 032 Set 3 p156–160 | Vehicles still in network at end = censored | No |
| Discrete-time logit hazard; competing risks | logit per interval | Departure in 30-min bins; delay ending in home/shop/recreation | 020 p627–629 | Per-interval exit probability | No |

### 2.9 Count models (safety)

| Method | Distribution | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|---|
| Poisson regression | mean = variance | Injury accidents at 3-leg stop junctions ~ major/minor AADT, median, driveways; also queue counts | 020 p631–651 | Conflicts ~ sim flows → safety performance function | Yes: `analyse_safety` |
| Overdispersion test → negative binomial | var = μ + αμ² | Same accidents (α = 0.516) | 020 p654–662 | Conflict counts per junction-hour | Yes: NB α, per road (`analyse_safety`) |
| Zero-inflated Poisson/NB + Vuong test; truncated Poisson | two-state | Zero-accident sections; run-off-road crashes; navigation use ≤ 5 days | 020 p651–678; 012 p76 | Rare-conflict sites | No |
| Poisson ML for gravity calibration | count likelihood (handles zeros) | Trip distribution | 009 p671–674 | Calibrate gravity β | β fitted to a mean trip length (Hyman), not Poisson ML |

### 2.10 Time series and state estimation

| Method | Assumption | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|---|
| **ARIMA / SARIMA** (ACF/PACF, Dickey-Fuller, Ljung-Box) | stationary after differencing | 3-min loop flows, Athens, 144 sites (MAPE ≈ 12–14 %); detector flow reconstruction | 020 p458–493; 007 p136–141; 003 p304 | Detector flows → 1-step forecast for predictive signals | Holt instead (`forecast`) |
| Exponential smoothing, Holt-Winters, decomposition | additive / multiplicative season | Traffic near congestion; monthly crashes; daily seasonality | 020 p441–458; 007 p88–91 | Light per-detector forecaster | Yes: `forecast` |
| **Kalman filter** (and EKF, particle, PHD filters) | linear-Gaussian state space | Multivariate traffic forecasting with missing data; bus arrival ETA from AVL; ACC sensor smoothing | 020 p489–490; 012 p31, p74; 009 p212, p219; 007 p167–170 | Network state from noisy/missing detectors; bus ETA | No |
| Granger causality; distributed lags; VAR / transfer function | lag regressions, F | Subsidies vs performance; lagged demand | 020 p310–314, p489 | Does upstream flow drive downstream queue? | No |
| Markov-switching AR, threshold AR, GARCH | hidden regimes | Free-flow vs congested regimes (no transport example) | 020 p475–488 | Regime detection | No |
| Graph Markov process (adjacency-masked transition) | Markov on graph | Network speed forecasting with missing values (PEMS-BAY) | 013 p164–170 | Detector speeds → next interval, imputed gaps | No |
| Wavelets, moving average, Butterworth | — | Bottleneck detection on loop data; denoising | 013 p24, p32 | Detector series → bottleneck flag | No |
| Oblique cumulative N- and T-curves | — | Locating active bottlenecks, queue onset | 009 p165–176 | Neighbouring detectors → bottleneck and onset time | No |

### 2.11 Queueing and stochastic processes

| Method | Assumption | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|---|
| Cumulative arrival/departure curves, Little's law L = λW | deterministic / fluid | Queue, delay (area), travel time at bottlenecks, toll plazas, signals | 009 p123, p131–136; 004 p282–298 | Two detectors → queue, delay; > 30 s between sensors → congestion → reroute | Queue measured (< 5 km/h) |
| D/D/1 signal queue | constant rates | Queue duration and length; oversaturation Q = T(v − c)/(N·d) | 011 p94–96 | Analytic benchmark for sim queues | No |
| **Signal delay d = d1·PF + d2 + d3** (Webster uniform + random/overflow, k, I = var/mean of arrivals per cycle) | Poisson-like arrivals per cycle | Lane-group delay and LOS | 011 p317–321, p368–370; 009 p125–126, p183 | Benchmark sim control delay; I from counts per cycle | Yes: Webster, Synchro |
| Arrival type / platoon ratio R_p = P·C/g | proportion on green | Coordination quality → progression factor | 011 p171, p318, p353 | P(arrive on green) → R_p | No |
| **Percentile back of queue** Q% = f_B%·Q; 95th-pct queue at stop control; storage overflow P(n > N) = 0.005 | empirical factors / queue-length distribution | Turn-bay length, spillback | 011 p500–507, p548–550; 022 p789–792 | Sim queue percentiles vs storage → spillback warning | Yes: 95th-pct queue vs road length (`analyse_run`) |
| **Gap acceptance**: critical gap t_c, follow-up t_f; distributed critical gaps, impatience | exponential major-stream headways | Two-way stop, roundabout, permitted-left, pedestrian crossing capacity | 011 p176–177, p529–574, p660; 009 p183–184; 022 p736–737, p789 | Accepted/rejected gaps → t_c distribution; benchmark capacity | Yes in driver model; t_c not fitted |
| Queue-free probability p0 = 1 − v/c, impedance products | independence, then corrected | Priority-rank impedance; all-way stop degree-of-conflict | 011 p535–537, p560–569 | — | No |
| Platoon dispersion | — | Blocked/unblocked periods near signals | 011 p539–545 | — | No |
| Diffusion approximation, relaxation time | stochastic queue near capacity | Steady-state formulas fail near v/c ≈ 1 | 009 p128, p137 | Warns when to trust analytic delay | No |
| Mamdani/Sugeno fuzzy controller premise | uniformly distributed arrival times | See 2.15 | 004 p150 | — | No |

### 2.12 Network assignment and spatial interaction

| Method | Distribution | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|---|
| Gravity model (un-, singly-, doubly-constrained; exponential vs power deterrence) | Gamma heterogeneity → power law | Trip distribution / OD matrix | 009 p668–674; 030 p650–658; 008 p63–65 | OD costs → OD matrix | Yes: `generate_demand`, decay fitted by `mean_trip_km` |
| Cross-classification trip rates | — | Trip generation | 009 p663–667 | Zone traits → productions | Institutions: one trip each way a person by shift (no rates yet) |
| Probit / logit stochastic user equilibrium; Dial's loading | normal / Gumbel perceptions | Assignment with perception error | 009 p36, p380–381; 029 p107–112 | Link costs → SUE flows | Path-size logit + MSA (`assign_routes`) |
| **Method of successive averages** with Monte Carlo loading | Σ step = ∞, Σ step² < ∞ | Solving SUE; day-to-day route updating | 009 p391–392 | Iterate runs → equilibrium routes | Yes: `assign_routes` |
| Logit link-use probability for critical links; expected network performance Σ P(S)·VKT(S) | logit; independent failures | Vulnerability: which link closure hurts most | 029 p107–112, p204 | Batch closures → ranked critical links | Static (`critical_links`) |
| Accessibility (gravity, cumulative, logsum), accessibility elasticity, percentiles | — | Land-use review, equity | 008 p63–75, p89–92; 030 p622, p635 | OD times → accessibility | No |

### 2.13 Bayesian methods

| Method | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|
| Bayes' theorem | P(car / age); P(incident / alarm) | 014 p99–101; 033 §3.8 | Update shares from observations | No |
| **Bayesian network incident detection** (CPTs from occupancy and queue states per cycle) | Arterial incidents from upstream and stop-line detectors | 029 p212–216 | Detector states → P(incident) | No |
| Spatiotemporal Bayesian MARS (lit.) | Short-term freeway flow from upstream segments | 007 p26 | — | No |

### 2.14 Multivariate, latent, ML and reinforcement learning

| Method | Used for | Sources | Simulator tool | Pure Python? | In ACMS |
|---|---|---|---|---|---|
| PCA, correlation screening | Reducing survey or day attributes | 020 p495–521; 027 p42, p48; 013 p49 | Compress KPIs / scenario attributes | Yes | No |
| Factor analysis, SEM | HOV attitudes; transit satisfaction | 020 p521–553; 028 p91–99 | Low (attitudinal) | Moderate | No |
| **K-means "travel conditions"** (+ hierarchical, EM; k up to √(n/2)) | 196 days → 6 clusters by demand, weather, incidents; frequency-weighted results | 027 p42–46, p91, p102–105; 013 p50, p217; 014 p164–184 | Many runs/days → weighted scenario set | Yes | Yes: `cluster_conditions` |
| DBSCAN | Taxi hotspots, motion regions | 013 p188, p202 | Conflict or queue-tail hotspots | Yes (O(n²)) | No |
| CHAID / decision trees (χ² splits) | Crash injury severity factors; mode classification | 007 p87–92; 014 p147–150 | Conflict severity ~ approach, phase, time | Yes | No |
| kNN, naive Bayes, SVM, random forest, boosting + SHAP | Fatalities by state; GPS mode detection; accident severity | 014 p123–143; 007 p68–79, p222–243 | Vehicle-class or state classification | kNN/NB yes; others moderate | No |
| **MDP / Q-learning signal control** (state: lane queues, waits, phase; reward: queue or pressure) | Traffic signal control; multi-agent RL bus holding (reward −CV²) | 013 p109–127; 003 p312; 007 p105, p116–118 | Frame → phase decision | Tabular yes; deep nets no | Signal-override hook exists |
| GA over signal settings with sim (or surrogate) as fitness; Shapley ranking of junctions | ~10⁵ runs of settings → total red wait | 007 p146–158; 004 p163–164 | Offsets/splits → fitness via headless runs | GA yes; GNN no | Yes: `optimise_signals` (splits, cycle) |
| LSTM / GCN / GAN forecasters | Corridor volume, speed, parking; physics-informed state estimation (conservation residual) | 013 p65–187; 014 p200 | Conservation residual as data check | No (deep nets) | No |

### 2.15 Fuzzy logic and optimisation under uncertainty

| Method | Used for | Sources | Simulator tool | In ACMS |
|---|---|---|---|---|
| **Mamdani/Sugeno fuzzy signal controller** (triangular/trapezoidal memberships, 37 rules) | Green, cycle, lane priority ~ density, waiting time, pedestrians, priority | 004 p150–157 | Lane density, waits → green times | No |
| Fuzzy c-means (+ SA-GA) | Grouping traffic-flow states | 012 p74 | Regime clusters | No |
| Fuzzy transportation LP (Mehar methods) | OR shipping problem; structurally like OD balancing, no traffic | 016 p16–29 | Not applicable directly | — |
| Stochastic programming, chance constraints, minimax regret | Ground holding, facility location, fleet repositioning | 009 p129, p325–357, p498 | Plan robust across demand scenarios | No |
| Littlewood / EMSR (normal demand), binomial show-ups | Seat protection (analogue: HOT lanes, parking quotas) | 009 p604–648 | Low | No |
| Risk = P(event) × consequence; scenario probability | Network risk, flood risk | 029 p53–55, p204, p273 | Sim consequence × given probability | No |

## 3. What a stdlib-only implementation needs

- Already available: `statistics` (mean, stdev, quantiles, median, `correlation`, `linear_regression`,
  `NormalDist().cdf/inv_cdf`), `math.lgamma`, `math.erf`, seeded `random.Random` (gammavariate, expovariate,
  lognormvariate, weibullvariate). `experiments.T95` holds two-sided 95 % t quantiles for 1–30 df.
- Hand-written in `stats.py`: t quantiles at any confidence level and the χ² tail (regularized incomplete
  beta / gamma from `math.lgamma`, inverted by bisection). Also built: the F tail, OLS, Poisson IRLS, k-means. Still needed: logit
  ML (Newton–Raphson) to estimate choice models from data. Permutation or bootstrap tests with `random` avoid
  quantiles altogether.
- Impractical: deep networks (LSTM, GCN, GAN, DQN), SVM at scale.

## 4. Shortlist of tools for the agent, from the catalogue

Each tool reads what a run already records (frames, `results.RunRecorder` minutes, roads and trips) or adds a
detector reading. All rows below are new unless marked.

1. **Replications needed** (027 p89–91): 4 pilot seeds → N = (t·s/(μ·e))², flag N > 20. Extends `run_experiment`. *Built.*
2. **Paired comparison on common seeds** (033 §9.3; 020 p108), next to the existing Welch interval; **ANOVA
   + Bonferroni** for ≥ 3 plans (033 §10.2–10.3). *Built: paired and Bonferroni; ANOVA not.*
3. **Headway and arrival checks**: χ² goodness of fit vs Poisson/exponential and variance/mean ratio I
   (020 p160; 011 p318; 031 p12), validating the arrival generator. *Built: I and its χ² test per origin (`analyse_run`).*
4. **Detector statistics**: space-mean speed, v85, % headways < 3 s, occupancy → density, saturation
   headway and lost time (009 p162–183; 011 p90, p236; 022 p119). *Built (`read_detectors`).*
5. **Percentile queues vs storage** → spillback warning (011 p500–507; 022 p789–792); **HCM control delay**
   as a benchmark for simulated delay (011 p317–321, code in `signal_timing`). *Built: percentile queues; not the delay benchmark.*
6. **Travel-time reliability**: PTI, TTI, SD across seeds (027 p92–93; 029 p47). *Built.*
7. **Calibration checker**: variation envelopes and Criteria I–IV (027 p75–80), with GEH/MAPE scoring
   (020 p490). *Built: GEH, RMSE, MAPE, slope, R² (`check_calibration`); not the envelopes.*
8. **Short-term forecaster per detector**: exponential smoothing, then SARIMA or a Kalman filter
   (020 p441–493; 007 p88–141) for predictive signal control. *Built: Holt (`forecast`); not SARIMA or Kalman.*
9. **Mode and route choice**: MNL with the HCM default utility (011 p1058), Path Size for overlapping
   routes, MSA day-to-day updating (009 p35–36, p391); probit only if correlation matters. *Built: MNL mode choice
   (`choose_modes`); route choice not.*
10. **Safety surrogate**: conflict counts → Poisson/NB safety performance function, speed–risk power law,
    blackspot ranking (020 p631–662; 009 p92; 024 p148). *Built (`analyse_safety`, `speed_risk`).*
11. **Signal-control learners**: tabular Q-learning (013 p121–122), fuzzy controller (004 p150–157), GA over
    offsets with headless runs (007 p146–158), all through the existing `signal_override` hook. *Built: the GA over
    cycles and splits (`optimise_signals`); not Q-learning, the fuzzy controller or offsets.*
12. **Scenario portfolio**: k-means travel conditions with frequency weights (027 p42–46, p91). *Built (`cluster_conditions`).*
