"""
Integration tests against a REAL MongoDB replica set (multi-document transactions).

Skipped unless LOCAL_MONGO_URI is set, e.g. after `python local_mongo.py up`:

    LOCAL_MONGO_URI="mongodb://127.0.0.1:27017/?replicaSet=rs0" python -m pytest tests/test_local_mongo_integration.py

Uses a throw-away database (never the seeded `tn_bus_lost_found`) and drops it afterwards.
These exercise the code path the mongomock suite cannot: claims.txn on a deployment that
supports transactions, where a failure mid-handover is undone by MongoDB itself.
"""
import os
import threading

import pytest
from pymongo import MongoClient

from claims import txn
from conftest import F3, FakeClock, RecordingSmsProvider

URI = os.environ.get("LOCAL_MONGO_URI")
pytestmark = pytest.mark.skipif(not URI, reason="set LOCAL_MONGO_URI to a replica-set MongoDB to run")
TEST_DB = "tn_bus_lost_found_txn_test"


@pytest.fixture
def real_db():
    client = MongoClient(URI, serverSelectionTimeoutMS=5000)
    client.drop_database(TEST_DB)
    db = client[TEST_DB]
    db.command("ping")
    yield db
    client.drop_database(TEST_DB)
    client.close()


@pytest.fixture
def f3_real(real_db, tmp_path):
    from seed_bookings import seed_bookings
    clock = FakeClock()
    seed_bookings(real_db, now=clock())
    return F3(real_db, clock, RecordingSmsProvider(), tmp_path)


def test_replica_set_supports_transactions(real_db):
    assert txn.supports_transactions(real_db)


def test_real_transaction_aborts_every_write(real_db, monkeypatch):
    real_db.a.insert_one({"_id": 1, "v": "before"})
    sessions = []
    real_init = txn.Tx.__init__

    def spy(self, db, session):
        sessions.append(session)
        real_init(self, db, session)

    monkeypatch.setattr(txn.Tx, "__init__", spy)

    def work(tx):
        tx.cas_update("a", {"_id": 1, "v": "before"}, {"$set": {"v": "after"}}, "x")
        tx.insert("b", {"_id": 9})
        raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        txn.run_atomically(real_db, work)
    assert sessions and all(s is not None for s in sessions)     # a real session, not the fallback
    assert real_db.a.find_one({"_id": 1})["v"] == "before" and real_db.b.count_documents({}) == 0


def _pickup(f3, token, tag, staff=None):
    staff = staff or f3.staff_client()
    return staff.post("/pickup/complete", json={"token": token, "qrTagId": tag, "idChecked": True})


@pytest.mark.parametrize("fail_at", ["custody_log", "matches", "lost_reports"])
def test_handover_failure_is_rolled_back_by_mongodb(f3_real, monkeypatch, fail_at):
    f3 = f3_real
    client, claim_id = f3.approved_claim()
    token = f3.token(client, claim_id)
    tag = f3.db.found_reports.find_one({"found_id": "item-1"})["qrTagId"]
    before = [f3.db[c].find_one() for c in ("claims", "found_reports", "matches", "lost_reports")]
    real_insert, real_cas = txn.Tx.insert, txn.Tx.cas_update

    def boom_insert(self, collection, doc):
        if collection == fail_at:
            raise RuntimeError("simulated outage")
        return real_insert(self, collection, doc)

    def boom_cas(self, collection, query, update, reason):
        if collection == fail_at:
            raise RuntimeError("simulated outage")
        return real_cas(self, collection, query, update, reason)

    monkeypatch.setattr(txn.Tx, "insert", boom_insert)
    monkeypatch.setattr(txn.Tx, "cas_update", boom_cas)
    with pytest.raises(RuntimeError):
        _pickup(f3, token["code"], tag)
    after = [f3.db[c].find_one() for c in ("claims", "found_reports", "matches", "lost_reports")]
    assert after == before and f3.db.custody_log.count_documents({"event": "handover"}) == 0
    monkeypatch.setattr(txn.Tx, "insert", real_insert)
    monkeypatch.setattr(txn.Tx, "cas_update", real_cas)
    assert _pickup(f3, token["code"], tag).status_code == 200
    assert f3.db.found_reports.find_one()["status"] == "handed_over"


def test_concurrent_pickups_on_real_transactions(f3_real):
    f3 = f3_real
    client, claim_id = f3.approved_claim()
    token = f3.token(client, claim_id)
    tag = f3.db.found_reports.find_one({"found_id": "item-1"})["qrTagId"]
    staffs = [f3.staff_client() for _ in range(5)]
    barrier, codes = threading.Barrier(5), []

    def go(staff):
        barrier.wait(timeout=10)
        codes.append(_pickup(f3, token["code"], tag, staff).status_code)

    threads = [threading.Thread(target=go, args=(s,)) for s in staffs]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes.count(200) == 1 and codes.count(409) == 4
    assert f3.db.custody_log.count_documents({"event": "handover"}) == 1


def test_full_claim_flow_on_real_mongodb(f3_real):
    f3 = f3_real
    client, claim_id = f3.approved_claim()
    assert f3.db.claims.find_one({"claimId": claim_id})["status"] == "token_issued"
    assert f3.db.found_reports.find_one()["status"] == "claim_locked"
    view = client.get(f"/claims/{claim_id}").get_json()["claim"]
    assert view["tokenAvailable"] and client.get(f"/claims/{claim_id}/token").status_code == 200
