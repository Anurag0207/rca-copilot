#!/usr/bin/env python3
"""
Seed one realistic incident across Elasticsearch, MongoDB and (optionally) Hive.

Scenario (T = incident start, all times UTC, deterministic given --seed):
  T-60 .. T     normal traffic, occasional benign errors, healthy DB pool
  T+1           checkout-api v2.4.1 deployed; the release sets DB pool size 20 -> 5   [MongoDB]
  T+3           db_pool_active climbs to the new ceiling of 5, db_pool_wait_ms explodes [Hive]
  T+4           "Connection is not available ... timed out" errors begin               [Elasticsearch]
  T+6           HTTP 500/503 responses to customers start                              [Elasticsearch]

The root cause the RCA should surface is the deploy shrinking the pool.

Connection settings come from the same env vars as the backend (ES_URL, ES_USERNAME,
ES_PASSWORD, ES_API_KEY, MONGODB_URI, HIVE_HOST, HIVE_PORT, ...). Re-running replaces
the demo data. Hive is skipped (with a warning) if PyHive or the cluster is unavailable.

Requires: elasticsearch==8.13.2, pymongo, and optionally pyhive.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

SERVICE = "checkout-api"
ES_INDEX = "app-logs-demo"
MONGO_DB = "rca_demo"
MONGO_COLLECTION = "deploy_events"
HIVE_DB = "rca_demo"
HIVE_TABLE = "service_metrics"

BASELINE_MINUTES = 60
INCIDENT_MINUTES = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--anchor", help="Incident start (ISO 8601 UTC). Default: now minus 30 minutes.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--skip-es", action="store_true")
    parser.add_argument("--skip-mongo", action="store_true")
    parser.add_argument("--skip-hive", action="store_true")
    return parser.parse_args()


def resolve_anchor(raw: str | None) -> datetime:
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).replace(second=0, microsecond=0)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return now - timedelta(minutes=INCIDENT_MINUTES + 10)


# ── data generation ─────────────────────────────────────────────────────────


def build_logs(t0: datetime, rng: random.Random) -> list[dict]:
    docs: list[dict] = []

    def log(ts: datetime, level: str, message: str, **extra) -> None:
        docs.append({"@timestamp": ts.isoformat(), "service": SERVICE, "level": level, "message": message, **extra})

    for minute in range(-BASELINE_MINUTES, INCIDENT_MINUTES + 1):
        base = t0 + timedelta(minutes=minute)
        for _ in range(rng.randint(4, 7)):
            ts = base + timedelta(seconds=rng.randint(0, 59))
            log(ts, "info", f"POST /checkout 200 in {rng.randint(40, 120)}ms", http_status=200)
        if minute % 15 == 0:  # benign background noise that exists before the incident too
            log(base + timedelta(seconds=30), "error", f"cache miss for session {rng.randint(1000, 9999)}", http_status=200)

        if minute >= 4:
            for _ in range(rng.randint(14, 24)):
                ts = base + timedelta(seconds=rng.randint(0, 59))
                log(
                    ts,
                    "error",
                    f"HikariPool-1 - Connection is not available, request timed out after {rng.randint(29000, 30500)}ms",
                    logger="com.zaxxer.hikari.pool.HikariPool",
                )
        if minute >= 6:
            for _ in range(rng.randint(10, 18)):
                ts = base + timedelta(seconds=rng.randint(0, 59))
                status = rng.choice([500, 503, 503])
                log(ts, "error", f"POST /checkout {status} in {rng.randint(30000, 31000)}ms", http_status=status)
    return docs


def build_deploys(t0: datetime) -> list[dict]:
    return [
        {"timestamp": t0 - timedelta(minutes=47), "service": "inventory-api", "kind": "deploy",
         "version": "v1.9.3", "author": "ci-bot", "message": "inventory-api v1.9.3 rolled out (unrelated)"},
        {"timestamp": t0 - timedelta(minutes=35), "service": SERVICE, "kind": "config_change",
         "version": "-", "author": "ops", "message": "checkout-api feature flag new_cart_ui enabled for 10% of users"},
        {"timestamp": t0 + timedelta(minutes=1), "service": SERVICE, "kind": "deploy",
         "version": "v2.4.1", "author": "ci-bot",
         "message": "checkout-api v2.4.1 rolled out; changes: spring.datasource.hikari.maximum-pool-size 20 -> 5"},
    ]


def build_metrics(t0: datetime, rng: random.Random) -> list[dict]:
    rows: list[dict] = []
    for minute in range(-BASELINE_MINUTES, INCIDENT_MINUTES + 1):
        ts = t0 + timedelta(minutes=minute)
        if minute < 3:
            active, wait, p99 = rng.randint(6, 10), rng.randint(2, 9), rng.randint(90, 140)
        else:
            active, wait, p99 = 5, rng.randint(24000, 30000), rng.randint(29000, 31000)
        for metric, value in (("db_pool_active", active), ("db_pool_wait_ms", wait), ("http_p99_ms", p99)):
            rows.append({"ts": ts, "service": SERVICE, "metric": metric, "value": float(value),
                         "message": f"{metric}={value}", "level": ""})
    return rows


# ── loaders ─────────────────────────────────────────────────────────────────


def seed_elasticsearch(docs: list[dict]) -> None:
    from elasticsearch import Elasticsearch, helpers

    kwargs: dict = {"hosts": [os.environ.get("ES_URL", "http://localhost:9200")]}
    if os.environ.get("ES_API_KEY"):
        kwargs["api_key"] = os.environ["ES_API_KEY"]
    elif os.environ.get("ES_USERNAME"):
        kwargs["basic_auth"] = (os.environ["ES_USERNAME"], os.environ.get("ES_PASSWORD", ""))
    client = Elasticsearch(**kwargs)

    client.indices.delete(index=ES_INDEX, ignore_unavailable=True)
    client.indices.create(
        index=ES_INDEX,
        mappings={"properties": {
            "@timestamp": {"type": "date"},
            "service": {"type": "keyword"},
            "level": {"type": "keyword"},
            "message": {"type": "text"},
            "http_status": {"type": "integer"},
            "logger": {"type": "keyword"},
        }},
    )
    helpers.bulk(client, ({"_index": ES_INDEX, "_source": d} for d in docs), refresh="wait_for")
    print(f"[elasticsearch] indexed {len(docs)} log documents into '{ES_INDEX}'")


def seed_mongo(docs: list[dict]) -> None:
    from pymongo import ASCENDING, MongoClient

    client = MongoClient(os.environ.get("MONGODB_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=5000)
    collection = client[MONGO_DB][MONGO_COLLECTION]
    collection.delete_many({})
    collection.insert_many(docs)
    collection.create_index([("timestamp", ASCENDING), ("service", ASCENDING)])
    print(f"[mongodb] inserted {len(docs)} change events into {MONGO_DB}.{MONGO_COLLECTION}")


def seed_hive(rows: list[dict]) -> None:
    from pyhive import hive

    auth = {"PLAIN": "NONE"}.get(os.environ.get("HIVE_AUTH_MECHANISM", "PLAIN"), os.environ.get("HIVE_AUTH_MECHANISM"))
    connection = hive.Connection(
        host=os.environ.get("HIVE_HOST", "localhost"),
        port=int(os.environ.get("HIVE_PORT", "10000")),
        username=os.environ.get("HIVE_USERNAME") or None,
        password=os.environ.get("HIVE_PASSWORD") or None,
        auth=auth,
    )
    cursor = connection.cursor()
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {HIVE_DB}")
    cursor.execute(f"DROP TABLE IF EXISTS {HIVE_DB}.{HIVE_TABLE}")
    cursor.execute(
        f"CREATE TABLE {HIVE_DB}.{HIVE_TABLE} "
        "(ts TIMESTAMP, service STRING, metric STRING, value DOUBLE, message STRING, level STRING)"
    )
    values = ", ".join(
        f"(TIMESTAMP '{r['ts'].strftime('%Y-%m-%d %H:%M:%S')}', '{r['service']}', '{r['metric']}', "
        f"{r['value']}, '{r['message']}', '{r['level']}')"
        for r in rows
    )
    cursor.execute(f"INSERT INTO {HIVE_DB}.{HIVE_TABLE} VALUES {values}")
    connection.close()
    print(f"[hive] inserted {len(rows)} metric rows into {HIVE_DB}.{HIVE_TABLE}")


def main() -> int:
    args = parse_args()
    t0 = resolve_anchor(args.anchor)
    rng = random.Random(args.seed)
    failures = 0

    steps = [
        ("elasticsearch", args.skip_es, lambda: seed_elasticsearch(build_logs(t0, rng))),
        ("mongodb", args.skip_mongo, lambda: seed_mongo(build_deploys(t0))),
        ("hive", args.skip_hive, lambda: seed_hive(build_metrics(t0, rng))),
    ]
    for name, skipped, run in steps:
        if skipped:
            print(f"[{name}] skipped by flag")
            continue
        try:
            run()
        except Exception as exc:  # one unreachable store must not block seeding the others
            failures += 1
            print(f"[{name}] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)

    incident = {
        "service": SERVICE,
        "start": t0.isoformat(),
        "end": (t0 + timedelta(minutes=INCIDENT_MINUTES)).isoformat(),
        "symptom": "checkout-api is returning HTTP 500/503 errors to customers",
    }
    print("\nIncident to analyse (paste into the RCA page, or POST to /rca/analyze):")
    print(json.dumps(incident, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
