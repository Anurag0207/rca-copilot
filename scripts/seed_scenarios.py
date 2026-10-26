#!/usr/bin/env python3
"""
Seed evaluation scenarios into Elasticsearch and MongoDB.

Per scenario (isolated, so scenarios never contaminate each other):
  Elasticsearch  index   rca-eval-<name>              <- logs, bulk-indexed
  MongoDB        db      rca_eval_<name>
                   deploy_events                      <- change events
                   service_metrics                    <- metrics (Hive-shaped rows)

Metrics are written to MongoDB, not Hive: no Hive cluster is available in the local stack.
The rows have the same shape the Hive connector reads (ts, service, metric, value), but this
means the evaluation does not exercise the Hive connector.

Connection settings use the backend's env vars: ES_URL, ES_USERNAME, ES_PASSWORD, ES_API_KEY,
MONGODB_URI. Re-seeding a scenario replaces its data.

  python scripts/seed_scenarios.py --scale 100000
  python scripts/seed_scenarios.py --only bad_deploy_pool --scale 200000
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scenarios  # noqa: E402
from scenarios.base import Scenario  # noqa: E402

DEFAULT_SCALE = 100_000
CHANGES_COLLECTION = "deploy_events"
METRICS_COLLECTION = "service_metrics"

ES_MAPPING = {
    "properties": {
        "@timestamp": {"type": "date"},
        "service": {"type": "keyword"},
        "level": {"type": "keyword"},
        "message": {"type": "text"},
        "host": {"type": "keyword"},
        "http_status": {"type": "integer"},
    }
}


@dataclass
class SeedStats:
    scenario: str
    logs: int
    changes: int
    metrics: int
    seconds: float


def es_client():
    from elasticsearch import Elasticsearch

    kwargs: dict = {"hosts": [os.environ.get("ES_URL", "http://localhost:9200")], "request_timeout": 120}
    if os.environ.get("ES_API_KEY"):
        kwargs["api_key"] = os.environ["ES_API_KEY"]
    elif os.environ.get("ES_USERNAME"):
        kwargs["basic_auth"] = (os.environ["ES_USERNAME"], os.environ.get("ES_PASSWORD", ""))
    return Elasticsearch(**kwargs)


def mongo_client():
    from pymongo import MongoClient

    return MongoClient(os.environ.get("MONGODB_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=5000)


def seed_scenario(scenario: Scenario, scale: int, es, mongo) -> SeedStats:
    """Generate and load one scenario; returns counts read back from the stores, not assumed."""
    from elasticsearch import helpers

    started = time.monotonic()
    data = scenario.generate(scale)

    es.indices.delete(index=scenario.index_name, ignore_unavailable=True)
    # one shard, no replicas: a single-node eval cluster; refresh disabled during the bulk load
    es.indices.create(
        index=scenario.index_name,
        mappings=ES_MAPPING,
        settings={"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "-1"},
    )
    helpers.bulk(
        es,
        ({"_index": scenario.index_name, "_source": doc} for doc in data.logs),
        chunk_size=5000,
        request_timeout=120,
    )
    es.indices.put_settings(index=scenario.index_name, settings={"refresh_interval": "1s"})
    es.indices.refresh(index=scenario.index_name)
    logs_stored = es.count(index=scenario.index_name)["count"]

    mongo.drop_database(scenario.mongo_database)
    db = mongo[scenario.mongo_database]
    if data.changes:
        db[CHANGES_COLLECTION].insert_many(data.changes)
    metric_docs = [
        {"timestamp": row["ts"], "service": row["service"], "metric": row["metric"], "value": row["value"], "message": row["message"]}
        for row in data.metrics
    ]
    db[METRICS_COLLECTION].insert_many(metric_docs)
    for collection in (CHANGES_COLLECTION, METRICS_COLLECTION):
        db[collection].create_index([("timestamp", 1), ("service", 1)])

    return SeedStats(
        scenario=scenario.name,
        logs=logs_stored,
        changes=db[CHANGES_COLLECTION].count_documents({}),
        metrics=db[METRICS_COLLECTION].count_documents({}),
        seconds=round(time.monotonic() - started, 1),
    )


def drop_scenario(scenario: Scenario, es, mongo) -> None:
    es.indices.delete(index=scenario.index_name, ignore_unavailable=True)
    mongo.drop_database(scenario.mongo_database)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scale", type=int, default=DEFAULT_SCALE, help="background log events per scenario (default %(default)s)")
    parser.add_argument("--only", nargs="*", help="scenario names (default: all)")
    args = parser.parse_args()

    selected = [scenarios.get(n) for n in args.only] if args.only else scenarios.load_all()
    es, mongo = es_client(), mongo_client()
    for scenario in selected:
        stats = seed_scenario(scenario, args.scale, es, mongo)
        print(f"{stats.scenario:<30} logs={stats.logs:>7} changes={stats.changes:>2} metrics={stats.metrics:>4} in {stats.seconds}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
