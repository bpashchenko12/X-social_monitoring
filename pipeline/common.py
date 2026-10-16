"""Shared helpers: paths, config, time handling, atomic JSON writes."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "queries.json")
DATA_DIR = os.path.join(ROOT, "docs", "data")
WEEKS_DIR = os.path.join(DATA_DIR, "weeks")
RAW_DIR = os.path.join(ROOT, "data", "raw")
LEDGER_PATH = os.path.join(ROOT, "data", "runs.jsonl")

JST = timezone(timedelta(hours=9))
UTC = timezone.utc


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_dt(value):
    """Parse X's 'Fri Nov 24 17:49:36 +0000 2023' or ISO-8601 into aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    s = str(value).strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            return datetime.strptime(s, fmt).astimezone(UTC)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def week_key(dt):
    """ISO week key (YYYY-Www) of a datetime in JST."""
    y, w, _ = dt.astimezone(JST).isocalendar()
    return f"{y}-W{w:02d}"


def write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_env_token(env_path=None):
    """APIFY_TOKEN from environment, else from git-ignored .env. Never logged."""
    tok = os.environ.get("APIFY_TOKEN")
    if tok:
        return tok.strip()
    env_path = env_path or os.path.join(ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("APIFY_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None
