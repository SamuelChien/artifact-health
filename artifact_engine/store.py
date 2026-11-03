"""Transactional checkpoints and immutable content storage for large collections."""
import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .contracts import digest


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    os.replace(temporary, path)


class Store:
    def __init__(self, run, create=False):
        self.run = Path(run).resolve()
        if create:
            self.run.mkdir(parents=True, exist_ok=False)
        if not self.run.is_dir():
            raise ValueError("evaluation run does not exist")
        self.db = sqlite3.connect(self.run / "state.sqlite", timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS folders(path TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, folder TEXT, sha256 TEXT,
                extension TEXT, bytes INTEGER, status TEXT, detail TEXT);
            CREATE INDEX IF NOT EXISTS file_content ON files(sha256,extension);
            CREATE INDEX IF NOT EXISTS file_status ON files(status);
            CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, sha256 TEXT, extension TEXT,
                status TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS artifact_status ON artifacts(status);
            CREATE TABLE IF NOT EXISTS units(id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL,
                data TEXT NOT NULL, FOREIGN KEY(artifact_id) REFERENCES artifacts(id));
            CREATE INDEX IF NOT EXISTS unit_artifact ON units(artifact_id);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, phase TEXT, state TEXT,
                attempts INTEGER DEFAULT 0, payload TEXT, result TEXT, error TEXT, updated_at TEXT);
            CREATE INDEX IF NOT EXISTS job_queue ON jobs(phase,state);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, pair_key TEXT, data TEXT,
                assessment TEXT, review TEXT);
            CREATE INDEX IF NOT EXISTS task_pair ON tasks(pair_key);
            CREATE INDEX IF NOT EXISTS task_component ON tasks(json_extract(data,'$.component'));
            CREATE TABLE IF NOT EXISTS links(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aliases(artifact_id TEXT PRIMARY KEY, canonical_artifact TEXT NOT NULL,
                basis TEXT NOT NULL);
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def event(self, event, **details):
        with (self.run / "progress.jsonl").open("a") as stream:
            stream.write(json.dumps({"time": now(), "event": event, **details}, ensure_ascii=False) + "\n")

    def snapshot(self, path):
        """Copy bytes while hashing; never hardlink mutable business originals."""
        path = Path(path)
        before = path.stat()
        blobs = self.run / "blobs"
        blobs.mkdir(exist_ok=True)
        h = hashlib.sha256()
        fd, name = tempfile.mkstemp(dir=blobs, prefix="pending-")
        try:
            with os.fdopen(fd, "wb") as out, path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    h.update(chunk)
                    out.write(chunk)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError("source changed while being copied")
            sha = h.hexdigest()
            destination = blobs / sha[:2] / (sha + path.suffix.lower())
            destination.parent.mkdir(exist_ok=True)
            if destination.exists():
                os.unlink(name)
            else:
                os.replace(name, destination)
            return sha, destination, after.st_size
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def add_artifact(self, identity, sha, extension, data):
        with self.db:
            self.db.execute("""INSERT INTO artifacts VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                             sha256=excluded.sha256, extension=excluded.extension, status=excluded.status, data=excluded.data""",
                            (identity, sha, extension, data["status"], json.dumps({**{k:v for k,v in data.items() if k!='units'},
                             'unit_count':len(data.get('units',[]))}, ensure_ascii=False, default=str)))
            self.db.execute("DELETE FROM units WHERE artifact_id=?", (identity,))
            self.db.executemany("INSERT INTO units VALUES (?,?,?)",
                                [(u["id"], identity, json.dumps(u, ensure_ascii=False, default=str)) for u in data.get("units", [])])

    def units(self, identifiers=None):
        if identifiers is None:
            rows = self.db.execute("SELECT id,data FROM units")
            return {r["id"]: json.loads(r["data"]) for r in rows}
        result = {}
        for identity in dict.fromkeys(identifiers):
            row = self.db.execute("SELECT data FROM units WHERE id=?", (identity,)).fetchone()
            if row:
                result[identity] = json.loads(row[0])
        return result

    def job(self, phase, payload):
        identity = digest([phase, payload, self.get("protocol")])
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(id,phase,state,payload,updated_at) VALUES (?,?,?,?,?)",
                            (identity, phase, "pending", json.dumps(payload, ensure_ascii=False), now()))
        return identity

    def queued(self, phase):
        return self.db.execute("SELECT * FROM jobs WHERE phase=? AND state='pending' ORDER BY rowid", (phase,)).fetchall()

    def finish(self, identity, result=None, error=None):
        with self.db:
            self.db.execute("UPDATE jobs SET state=?,result=?,error=?,updated_at=? WHERE id=?",
                            ("blocked" if error else "done", json.dumps(result, ensure_ascii=False), error, now(), identity))

    @contextlib.contextmanager
    def lock(self):
        import fcntl
        with (self.run / ".writer.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("another evaluator already owns this run") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
