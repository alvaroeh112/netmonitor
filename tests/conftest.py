import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from netmonitor.db import Database  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    yield d
    d.close()


def populate(db, now, hours=48, outage=None, step=30):
    """Datos sintéticos cada `step` s. outage=(inicio, fin, tipo) marca esos ciclos como caída."""
    start = now - hours * 3600
    with db.tx() as c:
        for ts in range(start, now, step):
            state = "ok"
            if outage and outage[0] <= ts < outage[1]:
                state = outage[2]
            lat = 15.0 + (ts // 600 % 5)
            down = state != "ok"
            c.execute("INSERT INTO cycles VALUES(?,?,?,?,?,?)", (ts, state, 1, 1, int(not down), 1))
            c.execute("INSERT INTO ping VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (ts, "gw", "gateway", 5, 5, 0.0, 0.5, 1.0, 2.0, 0.3))
            c.execute("INSERT INTO ping VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (ts, "1.1.1.1", "internet", 10, 0 if down else 10, 100.0 if down else 0.0,
                       None if down else lat - 1, None if down else lat, None if down else lat + 5,
                       None if down else 1.0))
            c.execute("INSERT INTO dns VALUES(?,?,?,?,?)", (ts, "gw", "google.com", 12.0, 1))
            c.execute("INSERT INTO http VALUES(?,?,?,?,?)", (ts, "https://x", 80.0, 204, 1))
        for ts in range(start, now, 5400):
            c.execute("INSERT INTO speed VALUES(?,?,?,?,?,?,?)",
                      (ts, 300.0 + (ts // 5400 % 3) * 10, 90.0, 12.0, "srv", 1, None))
        c.execute("INSERT INTO sysinfo VALUES(?,?,?,?,?,?)", (now - 100, "1.2.3.4", "192.168.1.1", 50.0, 0.3, 20.0))
        c.execute("INSERT INTO link VALUES(?,?,?,?,?,?,?,?,?,?)", (now - 100, "end0", 1000, "full", 1, 0, 0, 0, 0, 1))
        if outage:
            c.execute("INSERT INTO incidents(start_ts,end_ts,type) VALUES(?,?,?)", (outage[0], outage[1], outage[2]))
