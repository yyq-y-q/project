import sqlglot
from sqlglot import exp


class SQLValidator:

    BLACKLIST = [
        exp.Delete,
        exp.Drop,
        exp.Insert,
        exp.Update,
        exp.Alter,
        exp.Pragma,
        exp.Attach,
        exp.Detach
    ]

    def validate(self, sql):
        tree = sqlglot.parse(sql)

        if len(tree) != 1:
            raise ValueError("sql语句数量错误")

        if not isinstance(tree[0], exp.Select):
            raise ValueError(
                f"sql语句类型不合法：{type(tree[0]).__name__}"
            )

        for node in tree[0].walk():
            for ban in self.BLACKLIST:
                if isinstance(node, ban):
                    raise ValueError(
                        f"内有非法类型:{ban.__name__}"
                    )

        return tree[0]