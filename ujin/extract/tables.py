"""HTML table extraction: every ``<table>`` → a flat list of row dicts.

Each ``<table>`` on the page is parsed into one dict per data row::

    extract_tables('''
      <table>
        <tr><th>Name</th><th>Age</th></tr>
        <tr><td>Alice</td><td>30</td></tr>
      </table>''')
    # -> [{"Name": "Alice", "Age": "30"}]

Keying rules:

* If the first row carries any ``<th>`` it is treated as the header row and
  its (colspan-expanded) cell texts become the keys for every following row.
* A table with no header cells is *header-less*: every row is keyed positionally
  (``col0``, ``col1``, …).
* ``colspan``/``rowspan`` are expanded so each logical cell lands in its own
  grid slot — a value spanning two columns/rows is repeated into each.
* Nested tables are parsed as their own rows (and appended in document order);
  their text never leaks into the enclosing cell.

Rows from every table are concatenated in document order. Robust by contract:
empty or malformed input yields ``[]`` rather than raising. Uses selectolax
(``web``/``diff`` extra), like :mod:`ujin.extract.structured`.
"""
from __future__ import annotations

# A defensive ceiling on span attributes so a hostile ``colspan="99999999"``
# can't blow up memory while expanding the grid.
_MAX_SPAN = 1000


def extract_tables(html: str) -> list[dict]:
    """Parse every ``<table>`` in ``html`` into a flat list of row dicts.

    See the module docstring for the keying and span rules. Never raises: bad
    input or a parser hiccup returns whatever rows were assembled so far
    (``[]`` in the worst case).
    """
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html or "")
    except Exception:  # noqa: BLE001 — a parser failure must not propagate
        return []

    rows: list[dict] = []
    try:
        for table in tree.css("table"):
            rows.extend(_table_rows(table))
    except Exception:  # noqa: BLE001 — keep whatever we managed to parse
        return rows
    return rows


def _table_rows(table) -> list[dict]:
    """Parse a single ``<table>`` node into its data-row dicts."""
    raw = _direct_rows(table)
    if not raw:
        return []

    # Each parsed row is a list of (value, colspan, rowspan, is_header) tuples.
    parsed: list[list[tuple]] = []
    for tr in raw:
        cells = [
            (_cell_text(c), _span(c, "colspan"), _span(c, "rowspan"), c.tag == "th")
            for c in _row_cells(tr)
        ]
        parsed.append(cells)

    matrix = _expand(parsed)
    if not matrix:
        return []

    # The first row is the header row when at least one of its source cells is a
    # <th>; otherwise the whole table is header-less (positional keys).
    first_row_has_th = bool(parsed[0]) and any(cell[3] for cell in parsed[0])
    if first_row_has_th:
        keys = _make_keys(matrix[0])
        data = matrix[1:]
    else:
        keys = None
        data = matrix

    out: list[dict] = []
    for row in data:
        if not any(v for v in row):
            continue  # drop wholly-empty rows (spacers, rowspan phantoms)
        if keys is not None:
            row_dict = {
                (keys[i] if i < len(keys) else f"col{i}"): v
                for i, v in enumerate(row)
            }
        else:
            row_dict = {f"col{i}": v for i, v in enumerate(row)}
        out.append(row_dict)
    return out


def _direct_rows(table) -> list:
    """Return the ``<tr>`` nodes owned *directly* by ``table``.

    Rows are the table's direct ``<tr>`` children plus the ``<tr>`` children of
    its direct ``<thead>``/``<tbody>``/``<tfoot>`` sections. A nested table lives
    inside a cell, so its rows are never direct children here — that's how
    nesting stays cleanly separated without comparing node identities.
    """
    rows: list = []
    for child in table.iter(include_text=False):
        tag = child.tag
        if tag == "tr":
            rows.append(child)
        elif tag in ("thead", "tbody", "tfoot"):
            rows.extend(sub for sub in child.iter(include_text=False) if sub.tag == "tr")
    return rows


def _row_cells(tr) -> list:
    """Direct ``<td>``/``<th>`` children of a row (nested-table cells excluded)."""
    return [c for c in tr.iter(include_text=False) if c.tag in ("td", "th")]


def _cell_text(node) -> str:
    """Collapsed visible text of a cell, excluding any nested ``<table>``."""
    parts: list[str] = []

    def walk(n) -> None:
        for kid in n.iter(include_text=True):
            tag = kid.tag
            if tag == "-text":
                parts.append(kid.text(deep=False) or "")
            elif tag == "table":
                continue  # nested table is parsed on its own; don't inline it
            else:
                walk(kid)

    try:
        walk(node)
    except Exception:  # noqa: BLE001 — fall back to selectolax's own text()
        return " ".join((node.text(deep=True) or "").split())
    return " ".join("".join(parts).split())


def _span(node, attr: str) -> int:
    """Parse a ``colspan``/``rowspan`` attribute into a clamped int ≥ 1."""
    try:
        value = int((node.attributes.get(attr) or "1").strip())
    except (ValueError, TypeError, AttributeError):
        return 1
    if value < 1:
        return 1
    return min(value, _MAX_SPAN)


def _expand(parsed: list[list[tuple]]) -> list[list[str]]:
    """Expand colspan/rowspan into a rectangular matrix of cell strings.

    ``parsed`` is a list of rows, each a list of ``(value, colspan, rowspan,
    is_header)`` tuples. Returns a list of equal-width string rows; ``[]`` for a
    table with no cells.
    """
    n_rows = len(parsed)
    grid: dict[tuple[int, int], str] = {}
    max_col = -1
    for r, cells in enumerate(parsed):
        col = 0
        for value, colspan, rowspan, _is_header in cells:
            # Skip columns already claimed by an earlier cell's rowspan/colspan.
            while (r, col) in grid:
                col += 1
            # Never let a rowspan invent rows past the end of the table.
            rs = min(rowspan, n_rows - r)
            for dr in range(rs):
                for dc in range(colspan):
                    grid[(r + dr, col + dc)] = value
                    if col + dc > max_col:
                        max_col = col + dc
            col += colspan
    if max_col < 0:
        return []
    return [[grid.get((r, c), "") for c in range(max_col + 1)] for r in range(n_rows)]


def _make_keys(header_row: list[str]) -> list[str]:
    """Turn header-cell texts into unique, non-empty dict keys."""
    keys: list[str] = []
    seen: dict[str, int] = {}
    for i, value in enumerate(header_row):
        key = (value or "").strip() or f"col{i}"
        if key in seen:
            seen[key] += 1
            key = f"{key}_{seen[key]}"
        else:
            seen[key] = 1
        keys.append(key)
    return keys
