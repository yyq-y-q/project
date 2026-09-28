import sqlite3
import time

from .config import Config


class SQLExecutor:

    def connect(self):

        con = sqlite3.connect(
            f"file:{Config.QUERY_PATH}?mode=ro",
            uri=True
        )

        con.execute("PRAGMA query_only=ON")

        return con

    def execute(self, final_sql):

        con = None
        query_time_start = None
        return_rows = None

        try:

            con = self.connect()
            cur = con.cursor()

            time_out_start = time.time()

            def progress():

                if time.time() - time_out_start > Config.TIME_OUT:
                    return 1

                return 0

            con.set_progress_handler(
                progress,
                1000
            )

            query_time_start = time.time()

            cur.execute(final_sql)

            clomn = [d[0] for d in cur.description]

            rows = cur.fetchmany(Config.MAX_ROWS)

            return_rows = len(rows)

            result = [
                dict(zip(clomn, row))
                for row in rows
            ]

            duration = time.time() - query_time_start

            return (
                result,
                final_sql,
                duration,
                "success",
                return_rows,
                None
            )

        except Exception as e:

            if query_time_start:
                duration = time.time() - query_time_start
            else:
                duration = None

            return (
                [],
                final_sql,
                duration,
                "Faile",
                return_rows,
                str(e)
            )

        finally:

            if con:
                con.close()