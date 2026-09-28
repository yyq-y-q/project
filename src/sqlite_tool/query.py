from .logger import Logger
from .validator import SQLValidator
from .limiter import SQLLimit
from .executor import SQLExecutor


def query(sql):

    logger = Logger()

    final_sql = None

    try:

        validator = SQLValidator()

        tree = validator.validate(sql)

        limitor = SQLLimit()

        tree = limitor.add_limit(tree)

        executor = SQLExecutor()

        final_sql = tree.sql(
            dialect="sqlite"
        )

        (
            result,
            final_sql,
            duration,
            status,
            return_rows,
            error
        ) = executor.execute(final_sql)

        logger.logger_write(
            sql,
            final_sql,
            duration,
            status,
            return_rows,
            error
        )

        if error:
            return {"error": error}

        return result

    except Exception as e:
        print("发生异常：", e)

        logger.logger_write(
            sql,
            final_sql,
            None,
            "Faile",
            None,
            str(e)
        )
        return {"error": str(e)}

    finally:

        logger.close()


if __name__ == "__main__":
    # 只读网关冒烟（表需已由 ingest 归库）
    print(query("SELECT id, name, dept FROM employees LIMIT 5"))