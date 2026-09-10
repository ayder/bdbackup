"""Lightweight MySQL operations for tracking backup metadata."""

from __future__ import annotations

import logging
from typing import Any


class MySQLOps:
    """Manage backup metadata in a MySQL database.

    Requires PyMySQL to be installed (``pip install bdbackup[mysql]``).
    """

    def __init__(
        self,
        host: str = "localhost",
        user: str = "root",
        password: str = "",
        database: str = "backup_information",
    ):
        self.host = host
        self.user = user
        self.password = password
        self.database = database
        self.db: Any | None = None
        self.cursor: Any | None = None
        self.dblist: list[str] = []
        self.logger = logging.getLogger("bdbackup.db")
        self._connect()

    def _connect(self) -> None:
        try:
            import pymysql

            self.db = pymysql.connect(
                host=self.host,
                user=self.user,
                password=self.password,
                database=self.database,
            )
            self.cursor = self.db.cursor()
            self.logger.info("Connected to MySQL metadata database")
        except ImportError:
            self.logger.error(
                "PyMySQL is required for metadata tracking. "
                "Install it with: pip install bdbackup[mysql]"
            )
            raise
        except Exception as exc:
            self.logger.error("Unable to connect to MySQL: %s", exc)
            raise

    def get_dblist(self) -> list[str]:
        """Return the list of active databases from tbl_backup."""
        sql = "SELECT `ID`, `DATABASE`, `LAST_BACKUP_TIMESTAMP` FROM tbl_backup WHERE `STATUS` = 1"
        try:
            self.cursor.execute(sql)
            rows = self.cursor.fetchall()
            self.dblist = [row[1] for row in rows]
            for row in rows:
                self.logger.info("ID=%s, DB=%s, TIME=%s", row[0], row[1], row[2])
        except Exception as exc:
            self.logger.error("Unable to fetch database list: %s", exc)
            raise
        return self.dblist

    def update_db(self, dbname: str) -> None:
        """Update the last backup timestamp for a database."""
        sql = "UPDATE tbl_backup SET `LAST_BACKUP_TIMESTAMP` = NOW() WHERE `DATABASE` = %s"
        try:
            self.cursor.execute(sql, (dbname,))
            self.db.commit()
            self.logger.info("Updated backup timestamp for %s", dbname)
        except Exception as exc:
            self.db.rollback()
            self.logger.error("Unable to update %s: %s", dbname, exc)
            raise

    def close(self) -> None:
        """Close the database connection."""
        if self.db:
            self.db.close()
            self.db = None
            self.cursor = None

    def __del__(self) -> None:
        self.close()
