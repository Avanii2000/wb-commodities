import openpyxl, datetime

wb = openpyxl.load_workbook("CMO-Historical-Data-Monthly.xlsx", data_only=True)
ws = wb["Monthly Prices"]
rows = list(ws.iter_rows(values_only=True))

# find the header row that contains the commodity names
header_idx, cols = None, {}
for i, r in enumerate(rows[:15]):
    for j, c in enumerate(r):
        if isinstance(c, str):
            name = c.strip().lower()
            if name.startswith("aluminum"):
                cols["Aluminum ($/mt)"] = j
            if name.startswith("sugar, world"):
                cols["Sugar, World ($/kg)"] = j
    if len(cols) == 2:
        header_idx = i
        break
assert header_idx is not None, "Could not find Aluminum / Sugar, World columns"

# data rows look like 2026M10 in the first column
data = [r for r in rows[header_idx + 1:] if isinstance(r[0], str) and "M" in r[0] and r[0][:4].isdigit()]

lines = [f"# World Bank monthly prices (checked {datetime.date.today()})", ""]
for label, j in cols.items():
    valid = [(r[0], r[j]) for r in data if isinstance(r[j], (int, float))]
    last3 = valid[-3:]
    lines.append(f"**{label}**: " + ", ".join(f"{m}: {v}" for m, v in last3))
    lines.append("")
open("latest.md", "w").write("\n".join(lines))
print("\n".join(lines))
