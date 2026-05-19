"""SQL Server connection wrapper (pyodbc + ODBC Driver 18)."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import pandas as pd
import pyodbc
from dotenv import load_dotenv

load_dotenv()

ODBC_DRIVER = "{ODBC Driver 18 for SQL Server}"


class Database:
    def __init__(
        self,
        server: str | None = None,
        database: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self.server = server or os.environ["DB_SERVER"]
        self.database = database or os.environ["DB_NAME"]
        self.user = user or os.environ["DB_USER"]
        self.password = password or os.environ["DB_PASSWORD"]

    @property
    def connection_string(self) -> str:
        return (
            f"Driver={ODBC_DRIVER};"
            f"Server={self.server};"
            f"Database={self.database};"
            f"UID={self.user};PWD={self.password};"
            "TrustServerCertificate=yes;"
        )

    @contextmanager
    def get_connection(self) -> Iterator[pyodbc.Connection]:
        conn = pyodbc.connect(self.connection_string, autocommit=False)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params or ())
            return cur.rowcount

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        if not rows:
            return 0
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.fast_executemany = True
            cur.executemany(sql, rows)
            return cur.rowcount

    def query_df(self, sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params or ())
            cols = [c[0] for c in cur.description] if cur.description else []
            rows = cur.fetchall() if cols else []
            return pd.DataFrame.from_records([tuple(r) for r in rows], columns=cols)
