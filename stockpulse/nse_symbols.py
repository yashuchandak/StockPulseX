from __future__ import annotations

import csv
from pathlib import Path


def load_symbol_directory(path: Path) -> dict[str, str] | None:
    """Return SYMBOL -> company name from the optional NSE equity CSV."""
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"NSE symbol CSV has no header: {path}")
        headers = {name.strip().upper(): name for name in reader.fieldnames if name}
        symbol_col = headers.get("SYMBOL")
        company_col = headers.get("NAME OF COMPANY") or headers.get("COMPANY NAME")
        if not symbol_col:
            raise ValueError(f"NSE symbol CSV is missing a SYMBOL column: {path}")
        result: dict[str, str] = {}
        for row in reader:
            symbol = (row.get(symbol_col) or "").strip().upper()
            company = (row.get(company_col) or "").strip() if company_col else ""
            if symbol:
                result[symbol] = company
    return result

