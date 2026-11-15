#!/usr/bin/env python3
"""Approximate page length of each report section (IEEE two-column).

    python3 scripts/measure_pages.py report/main.pdf

Uses pdftotext -bbox to find where each numbered section heading sits.
Floats count toward the section in which they are placed.
"""
import re
import subprocess
import sys
import tempfile

pdf = sys.argv[1] if len(sys.argv) > 1 else "report/main.pdf"
with tempfile.NamedTemporaryFile(suffix=".html") as tmp:
    subprocess.run(["pdftotext", "-bbox", pdf, tmp.name], check=True)
    h = open(tmp.name).read()
pages = re.split(r"<page ", h)[1:]
names = {"Introduction": "Intro", "Problem": "Problem", "Objectives": "Objectives",
         "Related": "Related", "Literature": "LitSurvey", "Methodology": "Method",
         "Experimental": "Results", "Conclusion": "Concl"}
marks = []
for pi, p in enumerate(pages):
    m = re.match(r'width="([\d.]+)" height="([\d.]+)"', p)
    W, H = float(m.group(1)), float(m.group(2))
    words = re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', p)
    for i, (x0, y0, x1, y1, t) in enumerate(words):
        nxt = words[i + 1][4] if i + 1 < len(words) else ""
        if t in ("I.", "II.", "III.", "IV.", "V.", "VI.", "VII.") and nxt.title() in names:
            marks.append((names[nxt.title()], pi, float(x0), float(y0)))
        if t.title() == "References" and pi == len(pages) - 1:
            marks.append(("Refs", pi, float(x0), float(y0)))
        if t.startswith("Abstract") and pi == 0 and not marks:
            marks.append(("Abstract", pi, float(x0), float(y0)))
    if pi == len(pages) - 1:
        R = [float(w[3]) for w in words if float(w[0]) > W / 2]
        L = [float(w[3]) for w in words if float(w[0]) < W / 2]
top, bot = 54, H - 54
cp = lambda pi, x, y: pi * 2 + (0 if x < W / 2 else 1) + (y - top) / (bot - top)  # noqa: E731
pos = sorted([(n, cp(pi, x, y)) for n, pi, x, y in marks], key=lambda t: t[1])
end = cp(len(pages) - 1, W if R else 0, max(R) if R else max(L))
vals = [v for _, v in pos] + [end]
for i, (n, v) in enumerate(pos):
    print("%-9s %.2f pages" % (n, (vals[i + 1] - v) / 2))
lit = [i for i, (n, _) in enumerate(pos) if n in ("Related", "LitSurvey")]
k = lit[0] if lit else 2
print("abstract+intro(problem+objectives) = %.2f   total pages = %d" % ((vals[k] - vals[0]) / 2, len(pages)))
