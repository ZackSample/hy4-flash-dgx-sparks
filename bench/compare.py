#!/usr/bin/env python3
"""Side-by-side comparison of two or more bench.py result files (HTTP runs or offline --grade-answers runs).

  python3 compare.py results/hy4.json results/glm.json results/haiku.json     # writes results/compare.md
  python3 compare.py A.json B.json --out somewhere/compare.md
"""
import argparse
import json
import os
import sys

STATUS = {"pass": "PASS", "fail": "fail", "error": "ERROR", "unsupported": "unsupported", "no_answer": "no answer"}


def fmt(x, nd=1, unit=""):
    if x is None:
        return "-"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, float):
        return f"{x:.{nd}f}{unit}"
    return f"{x}{unit}"


def speed_rows(runs):
    """[(metric, [value per run], nd, unit)] for metrics present in at least one run."""
    S = [r.get("speed") or {} for r in runs]
    rows = []

    def add(name, vals, nd=1, unit=""):
        if any(v is not None for v in vals):
            rows.append((name, vals, nd, unit))

    for tid in ("decode-prose", "decode-code"):
        add(f"{tid}: median tok/s", [s.get(tid, {}).get("median_tps") for s in S])
        add(f"{tid}: median TTFT", [s.get(tid, {}).get("median_ttft") for s in S], 3, " s")
        add(f"{tid}: median tokens", [s.get(tid, {}).get("median_tokens") for s in S], 0)
    d = [s.get("decode-thinking", {}) for s in S]
    add("decode-thinking: tok/s", [x.get("tps") for x in d])
    add("decode-thinking: reasoning tokens", [x.get("reasoning_tokens") for x in d], 0)
    add("decode-thinking: answer tokens", [x.get("answer_tokens") for x in d], 0)
    add("decode-thinking: total time", [x.get("total_time") for x in d], 1, " s")
    add("decode-thinking: correct", [x.get("correct") for x in d])
    pre = [{r["target_tokens"]: r for r in s.get("prefill", [])} for s in S]
    for t in sorted(set().union(*[set(p) for p in pre])):
        k = f"prefill ~{t // 1000}k"
        add(f"{k}: prompt tokens", [p.get(t, {}).get("prompt_tokens") for p in pre], 0)
        add(f"{k}: TTFT", [p.get(t, {}).get("ttft") for p in pre], 2, " s")
        add(f"{k}: prompt tok/s", [p.get(t, {}).get("prompt_tps") for p in pre], 0)
        add(f"{k}: code word found", [p.get(t, {}).get("found") for p in pre])
    c = [s.get("cached-prefill", {}) for s in S]
    add("cached-prefill 16k: TTFT", [x.get("ttft") for x in c], 3, " s")
    add("cached-prefill 16k: speedup vs uncached", [x.get("speedup") for x in c], 1, "x")
    for n in ("2", "4"):
        pp = [(s.get("parallel") or {}).get(n, {}) for s in S]
        add(f"parallel x{n}: aggregate tok/s", [x.get("aggregate_tps") for x in pp])
        add(f"parallel x{n}: per-stream tok/s", [x.get("per_stream_tps_mean") for x in pp])
    return rows


def ability_tests(run):
    """Per-test rows; an HTTP run's decode-thinking (speed section) is shown as a 'thinking' test."""
    tests = list((run.get("ability") or {}).get("tests", []))
    dt = (run.get("speed") or {}).get("decode-thinking")
    if dt and not any(t["id"] == "decode-thinking" for t in tests):
        tests.append({"id": "decode-thinking", "category": "thinking",
                      "status": "error" if dt.get("error") else ("pass" if dt.get("correct") else "fail"),
                      "completion_tokens": dt.get("completion_tokens"), "total_time": dt.get("total_time")})
    return tests


def summary(tests):
    out = {}
    for t in tests:
        s = out.setdefault(t["category"], {k: 0 for k in list(STATUS) + ["total"]})
        s[t["status"]] = s.get(t["status"], 0) + 1
        s["total"] += 1
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", help="two or more result JSON files")
    p.add_argument("--out", default=None, help="markdown output (default: compare.md next to the first file)")
    args = p.parse_args(argv)
    if len(args.files) < 2:
        p.error("give at least two result files")
    runs = [json.load(open(f)) for f in args.files]
    labels = [r.get("meta", {}).get("label") or os.path.basename(f) for r, f in zip(runs, args.files)]
    if len(set(labels)) < len(labels):
        labels = [f"{l} ({chr(65 + i)})" for i, l in enumerate(labels)]
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.files[0])), "compare.md")
    two = len(runs) == 2

    L = [f"# {' vs '.join(labels)}", ""]
    for l, r in zip(labels, runs):
        m = r.get("meta", {})
        if m.get("mode") == "offline":
            L.append(f"- **{l}**: model `{m.get('model')}`, offline answers from `{m.get('answers_dir')}`, "
                     f"graded {m.get('finished')}")
        else:
            L.append(f"- **{l}**: model `{m.get('model')}`, started {m.get('started')}, think-off "
                     f"`{json.dumps(m.get('think_off'))}`, think-on `{json.dumps(m.get('think_on'))}`")

    rows = speed_rows(runs)
    if rows:
        head = "| metric | " + " | ".join(labels) + (f" | {labels[0]}/{labels[1]} |" if two else " |")
        L += ["", "## Speed", "", head, "|---" * (len(labels) + 1 + (1 if two else 0)) + "|"]
        for name, vals, nd, unit in rows:
            cells = [fmt(v, nd, unit) for v in vals]
            if two:
                a, b = vals
                ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (a, b)) and b
                cells.append(f"{a / b:.2f}x" if ok and "found" not in name and "correct" not in name
                             and "speedup" not in name and "prompt tokens" not in name else "")
            L.append(f"| {name} | " + " | ".join(cells) + " |")
        missing = [l for l, r in zip(labels, runs) if not r.get("speed")]
        if missing:
            L += ["", f"No speed section for: {', '.join(missing)}."]

    tests = [ability_tests(r) for r in runs]
    sums = [summary(t) for t in tests]
    cats = list(dict.fromkeys(c for s in sums for c in s))

    def cell(s):
        if not s:
            return "-"
        extra = [f"{s[k]} {STATUS[k].lower()}" for k in ("error", "unsupported", "no_answer") if s.get(k)]
        return f"{s['pass']}/{s['total']}" + (f" ({', '.join(extra)})" if extra else "")
    L += ["", "## Ability (pass counts)", "", "| category | " + " | ".join(labels) + " |",
          "|---" * (len(labels) + 1) + "|"]
    for c in cats:
        L.append(f"| {c} | " + " | ".join(cell(s.get(c)) for s in sums) + " |")
    tot = []
    for s in sums:
        tot.append(f"**{sum(x['pass'] for x in s.values())}/{sum(x['total'] for x in s.values())}**")
    L.append("| **all** | " + " | ".join(tot) + " |")
    common = set.intersection(*[{t["id"] for t in ts} for ts in tests])
    if any({t["id"] for t in ts} != common for ts in tests):
        cs = []
        for ts in tests:
            cs.append(f"{sum(1 for t in ts if t['id'] in common and t['status'] == 'pass')}/{len(common)}")
        L.append("| all, tests every run has | " + " | ".join(cs) + " |")

    maps = [{t["id"]: t for t in ts} for ts in tests]
    ids = list(dict.fromkeys(i for m in maps for i in m))

    def st(t):
        if not t:
            return "-"
        extra = []
        if t.get("completion_tokens") is not None:
            extra.append(f"{fmt(t.get('completion_tokens'), 0)} tok")
        if t.get("total_time") is not None:
            extra.append(f"{fmt(t.get('total_time'), 0)} s")
        return STATUS.get(t["status"], t["status"]) + (f" ({', '.join(extra)})" if extra else "")
    L += ["", "## Ability (per test)", "", "| test | " + " | ".join(labels) + " |", "|---" * (len(labels) + 1) + "|"]
    for i in ids:
        L.append(f"| {i} | " + " | ".join(st(m.get(i)) for m in maps) + " |")

    hmaps = [{h["id"]: h for h in (r.get("html") or [])} for r in runs]
    hids = list(dict.fromkeys(i for m in hmaps for i in m))
    if hids:
        def hc(h):
            if not h:
                return "-"
            if h.get("error"):
                return "ERROR"
            f = os.path.basename(h["file"]) if h.get("file") else "no html"
            return (f"{fmt(h.get('completion_tokens'), 0)} tok ({fmt(h.get('reasoning_tokens'), 0)} reasoning), "
                    f"{fmt(h.get('tps'))} tok/s, {fmt(h.get('total_time'), 0)} s, {h.get('finish_reason')}, `{f}`")
        L += ["", "## HTML pages (not graded)", "", "| page | " + " | ".join(labels) + " |",
              "|---" * (len(labels) + 1) + "|"]
        for i in hids:
            L.append(f"| {i} | " + " | ".join(hc(m.get(i)) for m in hmaps) + " |")

    text = "\n".join(L) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        f.write(text)
    print(text)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
