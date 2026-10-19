"""Where things are stored. Two places, so a platform can keep the database on fast local disk and bulky files elsewhere:

  DATA_DIR (default /data) ...... files: screenshots, signed authorization PDFs, broker lists, registry downloads
  DB_DIR / STATE_DIR (default: DATA_DIR) ... the SQLite database and small state: health file, update cache,
                                  generated key file, bulk-request scratch files

With neither set everything stays in /data exactly as before (Fly, Portainer). The specific variables (DB_PATH, SHOT_DIR,
AUTH_DIR, HEALTH_FILE, BROKER_OUT, BROKER_SRC, REGISTRY_DIR) still win when set. Never put DB_DIR on a network share:
SQLite needs local disk."""
import os

def _e(k): return (os.environ.get(k) or "").strip()

def data_dir():
    return _e("DATA_DIR") or (os.path.dirname(_e("DB_PATH")) if _e("DB_PATH") else "/data")

def db_dir():
    return _e("DB_DIR") or _e("STATE_DIR") or (os.path.dirname(_e("DB_PATH")) if _e("DB_PATH") else data_dir())

def db_path(): return _e("DB_PATH") or os.path.join(db_dir(), "optout.db")
def shot_dir(): return _e("SHOT_DIR") or os.path.join(data_dir(), "shots")
def auth_dir(): return _e("AUTH_DIR") or os.path.join(data_dir(), "auth")
def health_file(): return _e("HEALTH_FILE") or os.path.join(db_dir(), "site_health.json")
def broker_out(): return _e("BROKER_OUT") or os.path.join(data_dir(), "brokers_merged.yaml")
def broker_src(): return _e("BROKER_SRC") or os.path.join(data_dir(), "brokers")
def registry_dir(): return _e("REGISTRY_DIR") or os.path.join(data_dir(), "registries")
def update_cache(): return os.path.join(db_dir(), "update_check.json")
def key_file(): return os.path.join(db_dir(), "secrets", "ff_data_key")
def scratch_dir(): return db_dir()   # bulk-request configs and per-person eraser homes (personal data: keep off shared storage)

def summary():
    """For Heartbeat: where each kind of data lives (paths only)."""
    return {"files": data_dir(), "database": db_path(), "state": db_dir()}
