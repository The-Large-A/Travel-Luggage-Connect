"""
start.py — set up and start the whole project with one command.

    python start.py            # set up whatever is missing, then start the app
    python start.py --fresh    # also wipe every report, item, match and claim first
    python start.py stop       # stop the local database

What it does, in order (each step is skipped when already done):

  1. Python packages  creates ./venv and installs requirements.txt (first run:
                      several minutes; it re-runs only when requirements.txt changes)
  2. MongoDB          finds mongod (MONGODB_HOME, PATH, or a standard install); on
                      Windows it downloads the official MongoDB Community Server zip
                      into %USERPROFILE%\\mongodb if none is found (SHA-256 checked)
  3. Database         starts mongod as a single-node replica set on 127.0.0.1:27017
                      (data in ~/mongodb/data, outside the project)
  4. Demo data        seeds a fresh database with seed.py: depots, the transport
                      network, the mock Aadhaar and PNR databases. No old reports
                      or claims. Rebuilt automatically when seed.py's version changes.
  5. App              starts app.py on this computer AND the Wi-Fi network, so phones
                      on the same Wi-Fi can open the QR codes, and prints the logins

Only the Python standard library is used until step 1 is done. Your .env is read
but never changed; GROQ_API_KEY / OCR_API_KEY in it are optional extras.
"""
import argparse
import glob
import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(ROOT, "venv")
VENV_PY = os.path.join(VENV, "Scripts", "python.exe") if os.name == "nt" else os.path.join(VENV, "bin", "python")
REQUIREMENTS = os.path.join(ROOT, "requirements.txt")
HOME = os.environ.get("MONGODB_HOME") or os.path.join(os.path.expanduser("~"), "mongodb")
MONGO_PORT = int(os.environ.get("LOCAL_MONGO_PORT") or 27017)
REPLSET = "rs0"
LOCAL_URI = f"mongodb://127.0.0.1:{MONGO_PORT}/?replicaSet={REPLSET}"
APP_PORT = int(os.environ.get("PORT") or 5003)
MONGO_VERSION = "9.0.2"
MONGO_ZIP = f"mongodb-windows-x86_64-{MONGO_VERSION}.zip"
MONGO_URL = f"https://fastdl.mongodb.org/windows/{MONGO_ZIP}"

# The review build: no OTP or daily limits, OTPs shown in this window, phones on
# the Wi-Fi can log in over http. These override .env; SMS_PROVIDER does not
# (set SMS_PROVIDER=textbee in .env to send real SMS instead of printing them).
DEMO_ENV = {
    "SMS_MOCK_EXPOSE_OTP": "1",
    "OTP_RESEND_SECONDS": "0",
    "OTP_MAX_REQUESTS_PER_PNR_PER_HOUR": "1000000",
    "OTP_IP_REQUEST_LIMIT": "1000000",
    "OTP_IP_VERIFY_LIMIT": "1000000",
    "PNR_IP_LIMIT": "1000000",
    "OTP_MAX_ATTEMPTS": "10",
    "OTP_COOLDOWN_MINUTES": "1",
    "CLAIM_MAX_CLAIMS_PER_DAY": "1000000",
    "CLAIM_MAX_ANSWER_SUBMISSIONS_PER_DAY": "1000000",
    "CLAIM_IP_LIMIT": "1000000",
    "PASSENGER_COOKIE_SECURE": "0",
    "INTAKE_VISION_PROVIDER": "basic",
}


for _stream in (sys.stdout, sys.stderr):          # Windows consoles default to cp1252
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")   # and so do the processes started from here


def say(msg=""):
    print(msg, flush=True)


# ── 1. Python packages ───────────────────────────────────────────────────────
def in_venv() -> bool:
    return os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(os.path.abspath(VENV_PY))


def ensure_packages():
    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10 or newer is needed (3.11 recommended); this is {sys.version.split()[0]}.")
    if not os.path.exists(VENV_PY):
        say("[1/5] Creating the virtual environment (./venv)...")
        subprocess.check_call([sys.executable, "-m", "venv", VENV])
    stamp = os.path.join(VENV, ".requirements.sha256")
    want = hashlib.sha256(open(REQUIREMENTS, "rb").read()).hexdigest()
    have = open(stamp).read().strip() if os.path.exists(stamp) else ""
    if have != want:
        say("[1/5] Installing Python packages (first run: several minutes)...")
        subprocess.check_call([VENV_PY, "-m", "pip", "install", "--upgrade", "pip"], stdout=subprocess.DEVNULL)
        subprocess.check_call([VENV_PY, "-m", "pip", "install", "-r", REQUIREMENTS])
        with open(stamp, "w") as fh:
            fh.write(want)
    elif not in_venv():                            # said once, before switching into ./venv
        say("[1/5] Python packages: ready")


# ── 2. MongoDB ───────────────────────────────────────────────────────────────
def find_mongod():
    exe = "mongod.exe" if os.name == "nt" else "mongod"
    candidates = sorted(glob.glob(os.path.join(HOME, "*", "bin", exe)))
    if os.name == "nt":
        candidates += sorted(glob.glob(r"C:\Program Files\MongoDB\Server\*\bin\mongod.exe"))
    on_path = shutil.which("mongod")
    if on_path:
        candidates.append(on_path)
    return candidates[-1] if candidates else None


def download_mongod():
    if os.name != "nt":
        sys.exit("MongoDB was not found. Install MongoDB Community Server (macOS: `brew install "
                 "mongodb-community`; Linux: your package manager), or extract it under ~/mongodb, then re-run.")
    os.makedirs(os.path.join(HOME, "download"), exist_ok=True)
    zpath = os.path.join(HOME, "download", MONGO_ZIP)
    if not os.path.exists(zpath):
        say(f"[2/5] MongoDB not found. Downloading MongoDB Community Server {MONGO_VERSION} (about 1 GB)")
        say(f"      from {MONGO_URL}")
        tmp = zpath + ".part"
        with urllib.request.urlopen(MONGO_URL, timeout=60) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r      {done * 100 // total:3d}%", end="", flush=True)
        print()
        os.replace(tmp, zpath)
    with urllib.request.urlopen(MONGO_URL + ".sha256", timeout=60) as resp:
        expected = resp.read().decode().split()[0].lower()
    h = hashlib.sha256()
    with open(zpath, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != expected:
        os.remove(zpath)
        sys.exit("The MongoDB download did not match its published SHA-256. Run start.py again.")
    say("      Extracting...")
    with zipfile.ZipFile(zpath) as z:
        z.extractall(HOME)
    exe = find_mongod()
    if not exe:
        sys.exit(f"Could not find mongod.exe after extracting {zpath}.")
    return exe


def mongo_client(timeout_ms=3000, direct=False):
    from pymongo import MongoClient
    uri = f"mongodb://127.0.0.1:{MONGO_PORT}/?directConnection=true" if direct else LOCAL_URI
    return MongoClient(uri, serverSelectionTimeoutMS=timeout_ms)


def mongo_running() -> bool:
    try:
        mongo_client(1500, direct=True).admin.command("ping")
        return True
    except Exception:
        return False


# ── 3. Database ──────────────────────────────────────────────────────────────
def start_mongo():
    if mongo_running():
        say(f"[3/5] Database: already running on 127.0.0.1:{MONGO_PORT}")
    else:
        exe = find_mongod() or download_mongod()
        say("[2/5] MongoDB: " + exe)
        data, log = os.path.join(HOME, "data"), os.path.join(HOME, "mongod.log")
        os.makedirs(data, exist_ok=True)
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
        subprocess.Popen([exe, "--dbpath", data, "--port", str(MONGO_PORT), "--bind_ip", "127.0.0.1",
                          "--replSet", REPLSET, "--logpath", log, "--logappend"],
                         creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        for _ in range(120):
            if mongo_running():
                break
            time.sleep(0.5)
        else:
            sys.exit(f"MongoDB did not start; see {log}")
        say(f"[3/5] Database: started on 127.0.0.1:{MONGO_PORT} (data in {data})")
    admin = mongo_client(5000, direct=True).admin
    try:
        admin.command("replSetGetStatus")
    except Exception:
        admin.command("replSetInitiate", {"_id": REPLSET, "members": [{"_id": 0, "host": f"127.0.0.1:{MONGO_PORT}"}]})
    for _ in range(120):
        try:
            if mongo_client(2000).admin.command("hello").get("isWritablePrimary"):
                return
        except Exception:
            pass
        time.sleep(0.5)
    sys.exit("The database did not become ready; see ~/mongodb/mongod.log.")


def stop_mongo():
    if not mongo_running():
        say("The database is not running.")
        return
    try:
        mongo_client(3000, direct=True).admin.command("shutdown")
    except Exception:
        pass                                   # the connection closes as the server shuts down
    say("Database stopped.")


# ── 4. Demo data ─────────────────────────────────────────────────────────────
def local_secret(name, env_name):
    """A secret kept outside the project (stable across runs), unless .env already sets one."""
    from dotenv import dotenv_values
    from_env = (dotenv_values(os.path.join(ROOT, ".env")).get(env_name) or "").strip()
    if len(from_env) >= 32:
        return from_env
    path = os.path.join(HOME, name)
    if not os.path.exists(path):
        os.makedirs(HOME, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_urlsafe(48))
    return open(path, encoding="utf-8").read().strip()


def clear_uploads():
    uploads = os.path.join(ROOT, "static", "uploads")
    if os.path.isdir(uploads):
        for name in os.listdir(uploads):
            path = os.path.join(uploads, name)
            shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)


def seed_data(signing_secret, fresh):
    sys.path.insert(0, ROOT)
    os.environ["SIGNING_SECRET"] = signing_secret     # the vault derives its keys from it
    import seed
    client = mongo_client(10000)
    db = client[seed.DB_NAME]
    info = db["seed_info"].find_one({"_id": "seed"}) or {}
    if not fresh and info.get("version") == seed.SEED_VERSION:
        say("[4/5] Demo data: ready")
        return seed
    reason = "--fresh" if fresh else ("first run" if not info else "the demo data changed")
    say(f"[4/5] Demo data: building a fresh database ({reason}); old reports, items, matches and claims are removed")
    client.drop_database(seed.DB_NAME)
    clear_uploads()
    seed.seed_all(client[seed.DB_NAME], signing_secret)
    return seed


# ── 5. App ───────────────────────────────────────────────────────────────────
def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def run_app(seed):
    ip = lan_ip()
    env = dict(os.environ)                         # includes .env (loaded in main)
    env.update(DEMO_ENV)
    env.setdefault("SMS_PROVIDER", "mock")
    env.update(MONGO_URI=LOCAL_URI, SIGNING_SECRET=local_secret("signing_secret.txt", "SIGNING_SECRET"),
               SECRET_KEY=local_secret("flask_secret.txt", "SECRET_KEY"),
               HOST="0.0.0.0", PORT=str(APP_PORT), APP_BASE_URL=f"http://{ip}:{APP_PORT}")
    p, d = seed.REVIEW_LOGIN["passenger"], seed.REVIEW_LOGIN["depot"]
    say("\n[5/5] Starting the app (the AI models load first; this can take a minute).")
    say("=" * 72)
    say(f"  On this computer : http://localhost:{APP_PORT}")
    say(f"  Phones (same Wi-Fi): http://{ip}:{APP_PORT}   (QR codes point here)")
    say("")
    say(f"  Passenger login  : Aadhaar {p['aadhaar']}  →  PNR {p['pnr']}   ({p['name']})")
    say("                     The OTP is printed in this window: look for '[MOCK SMS]'.")
    say(f"  Depot staff login: {d['name']}, phone {d['phone']}, password {d['password']}")
    say("")
    say("  Stop everything with Ctrl+C.")
    say("=" * 72 + "\n")
    proc = subprocess.Popen([VENV_PY, os.path.join(ROOT, "app.py")], cwd=ROOT, env=env)
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        try:
            code = proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()
            code = 0
    stop_mongo()
    sys.exit(code if code not in (None, -2, 130, 3221225786) else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["run", "stop"], default="run")
    parser.add_argument("--fresh", action="store_true", help="wipe all reports, items, matches and claims first")
    args = parser.parse_args()
    ensure_packages()
    if not in_venv():                              # continue inside ./venv
        child = subprocess.Popen([VENV_PY, os.path.abspath(__file__)] + sys.argv[1:])
        while True:
            try:
                sys.exit(child.wait())
            except KeyboardInterrupt:              # Ctrl+C reaches the child too; let it shut down
                continue
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))        # same settings the app sees (e.g. data keys)
    if args.command == "stop":
        stop_mongo()
        return
    if not os.path.exists(os.path.join(ROOT, ".env")):
        say("No .env found: running with the built-in demo settings (AI extras that need API keys are off).")
    start_mongo()
    seed = seed_data(local_secret("signing_secret.txt", "SIGNING_SECRET"), args.fresh)
    run_app(seed)


if __name__ == "__main__":
    main()
