"""
local_mongo.py — run the project on a local MongoDB with the same seed data as the online database.

    python local_mongo.py up      # start mongod (single-node replica set) and seed everything
    python local_mongo.py app     # run app.py against the local database (OTPs shown in this terminal)
    python local_mongo.py status  # is mongod running? how many documents per collection?
    python local_mongo.py stop    # shut mongod down cleanly
    python local_mongo.py reset   # drop the local database and seed it again

MongoDB is expected as the portable Community Server build extracted under MONGODB_HOME
(default: %USERPROFILE%\\mongodb), i.e. MONGODB_HOME\\<version>\\bin\\mongod.exe. Data and logs
live in MONGODB_HOME\\data and MONGODB_HOME\\mongod.log — outside the repository and OneDrive.

Seeding replays the scripts used to set up the online database, in their original order:
seed_db.py (the 5 legacy depot logins) → seed_transport.py (deterministic: random.seed(42)) →
seed_bookings.py → seed_matches.py → seed_claims.py. Every script is idempotent.

The local connection string and SIGNING_SECRET are passed only to the processes started here,
so your .env (and its Atlas MONGO_URI) is never modified. SIGNING_SECRET comes from .env when
it is set there; otherwise one is generated once and kept in MONGODB_HOME\\signing_secret.txt,
because seeded claim answers and pickup codes are keyed with it and must stay stable.
"""
import argparse
import glob
import os
import secrets
import subprocess
import sys
import time

from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("MONGODB_HOME") or os.path.join(os.path.expanduser("~"), "mongodb")
PORT = int(os.environ.get("LOCAL_MONGO_PORT") or 27017)
REPLSET = "rs0"
LOCAL_URI = f"mongodb://127.0.0.1:{PORT}/?replicaSet={REPLSET}"
DB_NAME = "tn_bus_lost_found"


def _mongod_exe():
    found = sorted(glob.glob(os.path.join(HOME, "*", "bin", "mongod.exe")) +
                   glob.glob(os.path.join(HOME, "*", "bin", "mongod")))
    if not found:
        sys.exit(f"mongod not found under {HOME}\\<version>\\bin. Extract the MongoDB Community "
                 f"Server zip there, or set MONGODB_HOME.")
    return found[-1]


def _client(timeout_ms=3000, direct=False):
    from pymongo import MongoClient
    uri = f"mongodb://127.0.0.1:{PORT}/?directConnection=true" if direct else LOCAL_URI
    return MongoClient(uri, serverSelectionTimeoutMS=timeout_ms)


def _running():
    try:
        _client(1500, direct=True).admin.command("ping")
        return True
    except Exception:
        return False


def _signing_secret():
    from_env = (dotenv_values(os.path.join(ROOT, ".env")).get("SIGNING_SECRET") or "").strip()
    if len(from_env) >= 32:
        return from_env
    path = os.path.join(HOME, "signing_secret.txt")
    if not os.path.exists(path):
        os.makedirs(HOME, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_urlsafe(48))
        print(f"Generated a SIGNING_SECRET for local use in {path}")
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def _local_env(**extra):
    env = dict(os.environ)
    env.update(MONGO_URI=LOCAL_URI, SIGNING_SECRET=_signing_secret(), **extra)
    return env


def start():
    if _running():
        print(f"mongod already running on 127.0.0.1:{PORT}")
    else:
        data = os.path.join(HOME, "data")
        os.makedirs(data, exist_ok=True)
        log = os.path.join(HOME, "mongod.log")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
        subprocess.Popen([_mongod_exe(), "--dbpath", data, "--port", str(PORT), "--bind_ip", "127.0.0.1",
                          "--replSet", REPLSET, "--logpath", log, "--logappend"],
                         creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
        for _ in range(60):
            if _running():
                break
            time.sleep(0.5)
        else:
            sys.exit(f"mongod did not start; see {log}")
        print(f"mongod started on 127.0.0.1:{PORT} (data: {data})")
    admin = _client(5000, direct=True).admin
    try:
        admin.command("replSetGetStatus")
    except Exception:
        admin.command("replSetInitiate", {"_id": REPLSET, "members": [{"_id": 0, "host": f"127.0.0.1:{PORT}"}]})
        print(f"Initialised single-node replica set {REPLSET} (enables multi-document transactions)")
    for _ in range(60):
        try:
            if _client(2000).admin.command("hello").get("isWritablePrimary"):
                return
        except Exception:
            pass
        time.sleep(0.5)
    sys.exit("Replica set did not elect a primary; see the mongod log.")


SEEDS = ["seed_db.py", "seed_transport.py", "seed_bookings.py", "seed_matches.py", "seed_claims.py"]


def seed():
    env = _local_env()
    for script in SEEDS:
        print(f"\n== {script}")
        result = subprocess.run([sys.executable, os.path.join(ROOT, script)], cwd=ROOT, env=env)
        if result.returncode != 0:
            sys.exit(f"{script} failed (exit {result.returncode}).")


def status():
    if not _running():
        print(f"mongod is not running on 127.0.0.1:{PORT}")
        return
    client = _client(3000)
    hello = client.admin.command("hello")
    print(f"mongod {client.server_info()['version']} on 127.0.0.1:{PORT}, replica set "
          f"{hello.get('setName')}, primary={hello.get('isWritablePrimary')}")
    db = client[DB_NAME]
    for name in sorted(db.list_collection_names()):
        print(f"  {name:22} {db[name].estimated_document_count():>7}")


def stop():
    if not _running():
        print("mongod is not running")
        return
    try:
        _client(3000, direct=True).admin.command("shutdown")
    except Exception:
        pass                                  # the connection closes as the server shuts down
    print("mongod stopped")


def reset():
    start()
    _client(5000).drop_database(DB_NAME)
    print(f"Dropped database {DB_NAME}")
    seed()


def app():
    start()
    env = _local_env(SMS_MOCK_EXPOSE_OTP=os.environ.get("SMS_MOCK_EXPOSE_OTP", "1"))
    print(f"Starting app.py on {LOCAL_URI}. OTPs appear below as '[MOCK SMS] ... purpose=otp: <code>'.")
    sys.exit(subprocess.call([sys.executable, os.path.join(ROOT, "app.py")], cwd=ROOT, env=env))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["up", "app", "status", "stop", "reset", "seed"])
    cmd = parser.parse_args(argv).command
    if cmd == "up":
        start()
        seed()
        status()
    elif cmd == "seed":
        start()
        seed()
    else:
        {"app": app, "status": status, "stop": stop, "reset": reset}[cmd]()


if __name__ == "__main__":
    main()
