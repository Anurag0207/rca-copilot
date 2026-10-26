# Failure analysis of the baseline evaluation

Written **before** any fix was made. Baseline numbers: [evaluation_baseline.md](evaluation_baseline.md).
Raw stage outputs (planner fragments, correlator report, exact ranker input, raw LLM output) are in `docs/diagnosis/`, saved by
`scripts/diagnose.py` in `docs/diagnosis/<scenario>.json`.

Method: for each failing scenario, one full analysis was run against the same 100k-event data used in the
evaluation, with every stage's output saved. The stage that lost the answer is one of:

- **(a) planner**: the evidence was not fetched;
- **(b) correlator**: it was fetched but not surfaced in the report the ranker sees;
- **(c) ranker**: the ranker saw it but ranked or worded it badly.

Caveat: this is one diagnostic run per scenario. The LLM is non-deterministic, so a single run can differ from the
5-run baseline (see traffic_surge). Diagnoses below combine that run with the 5 baseline runs' titles.

| Scenario | Baseline strict top-1 | Stage that lost the answer |
|---|---|---|
| cpu_throttling_metrics_only | 0% | **(a) planner** |
| traffic_surge | 0% | **(a) planner**, with (c) wording |
| dropped_index | 0% | **(c) ranker** |
| dependency_outage | 40% | **(a) planner**, with (c) wording |
| cert_expiry | 40% | **(c) ranker** wording |

## cpu_throttling_metrics_only: (a) planner

- The metrics planner fragment was `{"collection": "service_metrics", "match": {"service": "recs-api", "metric": "cpu_usage"}}`.
  The metric is actually named `cpu_usage_cores`; the model invented a name from the symptom and the query returned **0 rows**.
- Result: the correlator saw no metrics at all (`metric_anomalies: []`), the ranker input contained only the log error spike
  and a gateway-timeout signature, and the ranker produced "Cache Misses Causing Timeout" from a single benign `cache miss` line.
- Evidence the correlator is not at fault: when run on the full generated data (unit test fixtures) it flags
  `container_cpu_limit_cores` (z=-15), `cpu_throttled_seconds_per_min` (z=1159) and `cpu_usage_cores` (z=-13).

## traffic_surge: (a) planner, plus (c) wording

- Planner: metrics fragment filtered to `metric in [request_rejections, error_rate]` (not real names) so **0 metric rows**;
  the logs fragment preferred `http_status` 429/503. The request-rate and CPU metrics never reached the correlator.
- Without metrics the only signals were log errors, so the ranker named the visible symptom: baseline titles were
  "Worker Queue Saturated" in 4 of 5 runs. (In the single diagnostic run the ranker happened to word it acceptably
  and scored rank 1; this is run-to-run variance, not a fix.)

## dropped_index: (c) ranker

- Fetched and surfaced correctly: the timeline contained both changes, the unrelated cache-TTL config change at -38 min and the
  deploy `v7.8.0 ... migration 0042 drops index idx_orders_created_at` at -6 min, plus the log error spike and signatures.
  (The metrics planner again invented `metric: "response_time"`, 0 rows, which also removed the DB latency signal.)
- The ranker ranked "Query timeout due to increased cache TTL" first in 5 of 5 baseline runs and the migration second.
  The input gave no relative information about the two changes (no lead time, nothing about whether an anomaly followed
  either one), so the ranker was choosing between two equally weighted changes.

## dependency_outage: (a) planner, plus (c) wording

- Planner: the logs fragment was `service = orders-api AND exists(http_status)`, which **excluded** the `ConnectException: Connection
  refused` and `circuit breaker ... OPEN` lines (they carry no `http_status`); the metrics fragment asked for a nonexistent
  `availability` metric (0 rows). Only the final `POST /orders 503 inventory unavailable` line survived.
- With so little evidence the ranker's titles drifted between "Inventory service unavailability" (kept the dependency) and
  generic "New error signature in app logs" (baseline failures).

## cert_expiry: (c) ranker wording

- All stages delivered: logs fetched (5,972 rows), the correlator surfaced the x509 expiry signature, the TLS handshake
  signature and the login-success-rate anomaly, and the ranker input contained every ground-truth keyword.
- The ranker titled the cause "Certificate Expiry Error" with a thin description that omitted the concrete error text, and listed
  "TLS Handshake Failure" separately (once above it). The strict check needs the evidence wording in the hypothesis; the answer
  was substantively right but terse.

## Outcome of the fixes (added after implementation)

Status: **partially complete.** The final re-evaluation was interrupted (machine restart) after 8 of 10 scenarios; those
interim numbers are in [PROJECT_STATS.md](PROJECT_STATS.md) and `evaluation_after_interim.md`. The fresh-scenario check
(`scripts/scenarios/fresh/`, 3 scenarios) has **not been run yet**.

What was kept: always-on broad context fetch merged with the planner's query; change-proximity fields (lead time,
followed-by-anomaly, nearest-preceding-change) in the ranker input; 90 s ranker timeout with one retry.

What was reverted, and why: the first ranker-prompt rewrite (trigger-naming titles, quote-the-evidence descriptions, explicit
accept/reject of the leading candidate, cite each relied-on source). Its first full evaluation run dropped `bad_deploy_pool`
from 4/5 to 1/5 strict, because the model returned a generic category label from the prompt's example list ("Resource Limit
Change") as the title. Ablations on that scenario (5 runs each): without the quote-the-evidence sentence 0/5; without the
leading-candidate requirement 2/5; baseline prompt with all data-side fixes 5/5; baseline prompt plus only the
leading-candidate requirement 0/5 (the word "deploy" moved into the separate verdict field, which the strict matcher does not
read). The whole prompt rewrite was therefore reverted to the baseline prompt, and a test now guards against the category
list returning. The matcher and scenario keywords were not changed.

Not demonstrated: that the ranker-prompt changes help. The interim improvements (dependency_outage 40% to 100%,
dropped_index 0% to 80%, traffic_surge 0% to 40% strict) came from the data-side changes, with the baseline prompt.
The CPU-throttling scenario, the one the planner fix most directly targets, had not yet run in the final configuration.

## Fixes chosen from this diagnosis (all general)

| Diagnosed cause | Fix | Stage |
|---|---|---|
| Planner invents field/metric values and narrows away evidence (4 of 5 scenarios) | Always run the broad, service-scoped context fetch; the LLM query becomes an additional fetch whose results are merged. For metric sources this fetches every metric for the service in the window. | planner/collector |
| Ranker cannot tell a recent change from a distant one | Timeline changes carry `lead_seconds`, `followed_by_anomaly` (anomaly within 15 min after) and `nearest_preceding_change`, computed from timing only | correlator |
| Ranker names symptoms; terse descriptions; no explicit judgement of the obvious suspect | Prompt: titles name the underlying trigger, descriptions quote error text/metric values, explicit accept/reject of the leading candidate, cite each relied-on source | ranker prompt |
| 231 s ranker call in one baseline run | 90 s per-call timeout, one retry, explicit warning | ranker call |

Not changed because the diagnosis did not support it: correlator metric-anomaly detection and timeline surfacing (stage (b)
lost nothing in the five scenarios), thresholds, scenario data and ground-truth keywords.

Note on prompt wording: the ranker prompt names generic trigger categories ("a change, a resource limit or capacity
exhaustion, an expired credential, a failing dependency"), as specified in the task. That list is conceptually close to some
scenario categories, though it contains none of the ground-truth keywords, so any improvement on those scenarios should be
read with that in mind. The fresh-scenario check (Step 4) is the test of whether the changes generalise.
