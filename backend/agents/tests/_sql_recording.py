"""Shared helper for tests that pin which SQL text and params a store sends."""


class RecordingConn:
    """Wraps a store's sqlite connection and records every execute() call."""

    def __init__(self, conn):
        self._conn = conn
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, list(params)))
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def only_statement(recorder, prefix):
    matching = [
        (sql, params)
        for sql, params in recorder.statements
        if sql.strip().startswith(prefix)
    ]
    assert len(matching) == 1, recorder.statements
    return matching[0]
