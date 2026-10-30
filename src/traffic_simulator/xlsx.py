"""A minimal Excel workbook writer (no Qt, standard library only): Office Open XML SpreadsheetML
(ECMA-376), the .xlsx format, as a zip of XML parts. Each sheet is a list of rows; numbers stay
numbers, everything else is text; the first row is bold and frozen; columns sized to their content."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

MAX_ROWS, MAX_CHARS = 1_048_576, 32_767  # Excel's limits: rows a sheet, characters a cell
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # not allowed in XML


def _column(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _cell(ref: str, value, style: int) -> str:
    if value is None or value == "":
        return ""
    styled = f' s="{style}"' if style else ""
    if isinstance(value, bool):
        value = "yes" if value else "no"
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):  # NaN or infinite: no number
            return ""
        return f'<c r="{ref}"{styled}><v>{value!r}</v></c>' if isinstance(value, float) else \
            f'<c r="{ref}"{styled}><v>{value}</v></c>'
    text = escape(_CONTROL.sub("", str(value))[:MAX_CHARS])
    return f'<c r="{ref}"{styled} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet(rows: list[list]) -> str:
    widths: dict[int, int] = {}
    lines = []
    for r, row in enumerate(rows[:MAX_ROWS], 1):
        cells = []
        for c, value in enumerate(row):
            cells.append(_cell(f"{_column(c)}{r}", value, 1 if r == 1 else 0))
            if r <= 200:  # widths from the first rows
                shown = f"{value:.4g}" if isinstance(value, float) else str(value if value is not None else "")
                widths[c] = max(widths.get(c, 0), min(len(shown), 60))
        lines.append(f'<row r="{r}">{"".join(cells)}</row>')
    columns = "".join(f'<col min="{c + 1}" max="{c + 1}" width="{w + 2}" customWidth="1"/>' for c, w in sorted(widths.items()))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
            'state="frozen"/></sheetView></sheetViews>'
            + (f"<cols>{columns}</cols>" if columns else "")
            + f'<sheetData>{"".join(lines)}</sheetData></worksheet>')


def write_xlsx(path: str | Path, sheets: list[tuple[str, list[list]]]) -> Path:
    """Write the sheets, (name, rows), to a workbook at `path` (written whole, then moved into place)."""
    names, used = [], set()
    for name, _ in sheets:
        # Excel's rules: no control characters or []:*?/\, no apostrophe at either end, at most 31 characters
        clean = base = re.sub(r"[\[\]:*?/\\]", " ", re.sub(r"[\x00-\x1f]", "", name)).strip(" '")[:31].rstrip(" '") \
            or "Sheet"
        number = 1
        while clean.lower() in used:  # taken (Excel ignores case): " 2", " 3"… on the name, cut to make room
            number += 1
            clean = base[:31 - len(f" {number}")] + f" {number}"
        used.add(clean.lower())
        names.append(clean)
    target = Path(path)
    part = target.with_suffix(".part")
    try:
        _write_parts(part, sheets, names)
    except BaseException:
        part.unlink(missing_ok=True)  # a failed write leaves nothing behind
        raise
    part.replace(target)
    return target


def _write_parts(part: Path, sheets: list[tuple[str, list[list]]], names: list[str]) -> None:
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr("[Content_Types].xml",
                      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                      '<Default Extension="xml" ContentType="application/xml"/>'
                      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                      '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                      + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.'
                                f'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets) + 1))
                      + "</Types>")
        book.writestr("_rels/.rels",
                      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                      'officeDocument" Target="xl/workbook.xml"/></Relationships>')
        book.writestr("xl/workbook.xml",
                      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                      '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                      + "".join(f'<sheet name="{escape(name, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                                for i, name in enumerate(names, 1))
                      + "</sheets></workbook>")
        book.writestr("xl/_rels/workbook.xml.rels",
                      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                      + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                                f'relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
                      + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/'
                        '2006/relationships/styles" Target="styles.xml"/></Relationships>')
        book.writestr("xl/styles.xml",
                      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                      '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                      '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
                      '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                      '<fills count="2"><fill><patternFill patternType="none"/></fill>'
                      '<fill><patternFill patternType="gray125"/></fill></fills>'
                      '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                      '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                      '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                      '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
                      '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                      '</styleSheet>')
        for i, (_, rows) in enumerate(sheets, 1):
            book.writestr(f"xl/worksheets/sheet{i}.xml", _sheet(rows))


_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_xlsx(path: str | Path) -> dict[str, list[list]]:
    """Each sheet's rows as values (numbers as floats, text as text), of a workbook write_xlsx wrote."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as book:
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        names = [sheet.get("name") for sheet in workbook.iter(_MAIN + "sheet")]
        sheets = {}
        for index, name in enumerate(names, 1):
            rows = []
            for row in ET.fromstring(book.read(f"xl/worksheets/sheet{index}.xml")).iter(_MAIN + "row"):
                values = []
                for cell in row.iter(_MAIN + "c"):
                    column = "".join(ch for ch in cell.get("r") if ch.isalpha())
                    position = sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(column))) - 1
                    values += [None] * (position - len(values))
                    text = cell.find(f"{_MAIN}is/{_MAIN}t")
                    values.append(text.text if text is not None else float(cell.find(_MAIN + "v").text))
                rows.append(values)
            sheets[name] = rows
    return sheets
