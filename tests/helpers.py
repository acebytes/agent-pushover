import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_pushover.config import DEFAULTS, write_json
from agent_pushover.engine import Store


def configured_home(path):
    home = Path(path)
    home.mkdir(parents=True, exist_ok=True)
    write_json(home / "config.json", copy.deepcopy(DEFAULTS))
    write_json(home / "credentials.json", {"user": "u" * 30, "token": "t" * 30})
    return home


def jobs(home):
    with Store(home) as store:
        return [dict(row) for row in store.db.execute("SELECT * FROM jobs ORDER BY due")]
