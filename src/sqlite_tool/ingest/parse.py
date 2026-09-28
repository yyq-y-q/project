"""
阶段 1 — 解析 parse

职责：文件字节 → RawTable（原始表头 + 原始行）
不做：改列名、改类型、去重、写库
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import load_workbook
#专门处理 .xlsx 的库

from .types import RawTable


def _cell_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()

                                     #可用 sheet 选页 
def parse_csv(path: Path | str) -> RawTable:
    """读 csv：第 1 行表头，其余数据行。"""
    import csv

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() != ".csv":
        raise ValueError(f"不是 csv: {path.suffix}")

    last_err: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                reader = csv.reader(f)
                try:
                    header_row = next(reader)
                except StopIteration as e:
                    raise ValueError(f"空表: {path}") from e
                headers = [_cell_str(c) for c in header_row]
                while headers and headers[-1] == "":
                    headers.pop()
                if not headers:
                    raise ValueError(f"无表头: {path}")
                width = len(headers)
                rows: list[list[Any]] = []
                for raw in reader:
                    vals = list(raw[:width])
                    while len(vals) < width:
                        vals.append(None)
                    rows.append(vals)
            return RawTable(
                source=path.resolve(),
                headers=headers,
                rows=rows,
                sheet="csv",
            )
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise ValueError(f"csv 编码无法识别: {path} ({last_err})")


def parse_table(path: Path | str, sheet: str | None = None) -> RawTable:
    """按后缀分流：xlsx/xlsm → excel；csv → csv。"""
    path = Path(path)
    suf = path.suffix.lower()
    if suf in {".xlsx", ".xlsm"}:
        return parse_excel(path, sheet=sheet)
    if suf == ".csv":
        return parse_csv(path)
    raise ValueError(f"不支持的表文件类型: {suf}")


def parse_excel(path: Path | str, sheet: str | None = None) -> RawTable:
    """
    读 xlsx：
    - 默认第一个 sheet
    - 第 1 行 = 表头
    - 其余 = 数据（整行全空则先留下，是否丢给 clean）
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
          #文件类型
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError(f"暂只支持 Excel: {path.suffix}")
     # 对象     打开工作簿的函数              尽量要算出来的值，不要公式字符串
    wb = load_workbook(path, read_only=True, data_only=True)
    #它读的是 Excel 二进制/压缩格式，不是普通 open().read() 能当表格用的纯文本。
    try:
        ws = wb[sheet] if sheet else wb.active
        sheet_name = ws.title

        it = ws.iter_rows(values_only=True)
        try:
            header_row = next(it)
        except StopIteration:
            raise ValueError(f"空表: {path}") from None

        headers = [_cell_str(c) for c in header_row]
        # 去掉表头尾部全空列（Excel 常见）
        while headers and headers[-1] == "":
            #扔掉最后一个
            headers.pop()
        if not headers:
            raise ValueError(f"无表头: {path}")

        width = len(headers)
        rows: list[list[Any]] = []
        for raw in it:
            if raw is None:
                continue
                #最多取 width 个；若原来不够，有多少给多少
            vals = list(raw[:width])
            while len(vals) < width:
                vals.append(None)
            rows.append(vals)
    finally:#关闭工作簿
        wb.close()

    return RawTable(
        source=path.resolve(),
        headers=headers,
        rows=rows,
        sheet=sheet_name,
    )
