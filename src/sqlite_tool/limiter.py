from sqlglot import exp

from .config import Config


class SQLLimit:

    def add_limit(self, tree):

        if tree.args.get("limit"):

            limit_node = tree.args["limit"]
            value = limit_node.expression

            if value:

                limit = int(value.name)

                if limit > Config.MAX_ROWS:
                    limit_node.set(
                        "expression",
                        exp.Literal.number(Config.MAX_ROWS)
                    )

            else:

                limit_node.set(
                    "expression",
                    exp.Literal.number(Config.MAX_ROWS)
                )

        else:

            tree.set(
                "limit",
                exp.Limit(
                    expression=exp.Literal.number(
                        Config.MAX_ROWS
                    )
                )
            )

        return tree