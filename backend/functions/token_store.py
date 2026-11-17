"""Private, per-function webhook credential storage."""

import hashlib
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class WebhookTokenStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS webhook_tokens (
                id TEXT PRIMARY KEY, label TEXT NOT NULL, function_id TEXT NOT NULL UNIQUE,
                graph_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, rotated_at TEXT
            )""")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def get(self, function_id):
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT id, label, function_id, graph_id, created_at, rotated_at FROM webhook_tokens WHERE function_id = ?",
                (function_id,),
            ).fetchone()
            return dict(row) if row else None

    def mint(self, function_id, graph_id, label):
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "INSERT INTO webhook_tokens VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    str(uuid.uuid4()),
                    label,
                    function_id,
                    graph_id,
                    self._hash(token),
                    now,
                ),
            )
        return token

    def rotate(self, function_id, graph_id, token_id):
        token = secrets.token_urlsafe(32)
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE webhook_tokens SET token_hash = ?, rotated_at = ? WHERE function_id = ? AND graph_id = ? AND id = ?",
                (
                    self._hash(token),
                    datetime.now(timezone.utc).isoformat(),
                    function_id,
                    graph_id,
                    token_id,
                ),
            )
            if not cursor.rowcount:
                return None
        return token

    def revoke(self, function_id, graph_id, token_id):
        with self._connect() as db:
            return bool(
                db.execute(
                    "DELETE FROM webhook_tokens WHERE function_id = ? AND graph_id = ? AND id = ?",
                    (function_id, graph_id, token_id),
                ).rowcount
            )

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def resolve(self, token):
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT id, label, function_id, graph_id, created_at, rotated_at FROM webhook_tokens WHERE token_hash = ?",
                (self._hash(token),),
            ).fetchone()
            return dict(row) if row else None
