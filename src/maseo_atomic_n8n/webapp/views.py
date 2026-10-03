"""What the MASEO web app page shows, worked out in Python.

The page has no JavaScript: everything it shows (durations, the queue
cards, the live pipeline graph, the step list with each step's files) is
made here from the jobs n8n returns, and templates/index.html only lays
it out. app.py calls card(), detail() and form_models().
"""
import html
import json
import math
import re
from datetime import datetime

from markupsafe import Markup

AGENTS = [
    ("CQAtomization", "Splits each question into atomic CLaRO questions"),
    ("TermIdentification", "Filters the extracted terms per question"),
    ("TermRefinement", "Refines the whole term set (batch level)"),
    ("AxiomGeneration", "Proposes axioms for the ontology"),
    ("TestGeneration", "Writes Themis tests (enables themis_test)"),
]
EFFORT_LEVELS = ["low", "medium", "high"]
ACTIVE = ("queued", "running")
ENDED = ("done", "failed", "cancelled")

# the graph node a job is in -> what the page calls it
CURRENT_LABELS = {
    "atomize_cqs": "CQAtomization", "extract_terms": "TermExtraction",
    "identify_terms": "TermIdentification", "refine_terms": "TermRefinement",
    "finalize_terms": "finalize terms", "generate_axioms": "AxiomGeneration",
    "generate_tests": "TestGeneration", "generate_ontology": "OntoGeneration",
    "advance": "advance",
}
STEP_LABELS = {
    "atomize_cqs": "CQAtomization", "extract_terms": "TermExtraction",
    "identify_terms": "TermIdentification", "refine_terms": "TermRefinement",
    "finalize_terms": "Requirements ready", "generate_axioms": "AxiomGeneration",
    "generate_tests": "TestGeneration", "generate_ontology": "OntoGeneration",
}
RUN_STAMP = re.compile(r"^\d{8}-\d{6}(_\d+)?$")


# ------------------------------------------------------------------ small --
def dur(s):
    """Seconds as 7s / 3m 05s / 1h 04m."""
    if s is None:
        return "–"
    try:
        s = max(0, round(float(s)))
    except (TypeError, ValueError):
        return "–"
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f"{h}h {m:02d}m" if h else f"{m}m {x:02d}s" if m else f"{x}s"


def clock(t):
    """A time stamp as 'Sep 25, 14:30' (the web app's time zone, TZ)."""
    if not t:
        return "–"
    d = datetime.fromtimestamp(t)
    return f"{d:%b} {d.day}, {d:%H:%M}"


def when(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def clean_name(value):
    """The Job Name the way the service stores it."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip()).strip("_")


def run_name(j):
    """(name, stamp): the Job Name strong and the date/time stamp muted."""
    rid, d = j.get("id") or "", j.get("domain") or ""
    if d and rid.startswith(d + "_") and RUN_STAMP.match(rid[len(d) + 1:]):
        return d, rid[len(d) + 1:]
    return rid, ""


def current_label(j):
    c = j.get("current")
    if not c:
        return "between steps"
    if c.get("node") == "check_tool":
        return "checking " + (c.get("tool") or "")
    if c.get("node") == "correct_ontology":
        return "OntoCorrection fixing " + (c.get("tool") or "")
    return CURRENT_LABELS.get(c.get("node"), c.get("node") or "")


def since(c, now):
    """How long the current step has run."""
    if not c:
        return None
    if now and c.get("since"):
        return now - c["since"]
    return c.get("seconds")


def progress(j):
    """0..1 for the progress bar of a running job."""
    if j["status"] == "done":
        return 1.0
    if j["status"] != "running":
        return 0.0
    steps = [s["node"] for s in j.get("steps") or [] if s.get("on")]
    passed = {h.get("node") for h in j.get("history") or []}
    n, total = sum(1 for s in steps if s in passed), len(steps) + 1
    p = j.get("position") or {}
    if p.get("round"):
        rounds = p.get("loop_rounds") or 3
        n += min(1, ((p["round"] - 1) + (p.get("tool_index") or 0) / max(1, len(j.get("tools") or []))) / rounds)
    return min(.98, n / total)


def count_fixes(j):
    n = sum(1 for h in j.get("history") or [] if h.get("node") == "correct_ontology" and not h.get("failed"))
    return f"{n}" + (" +1" if (j.get("current") or {}).get("node") == "correct_ontology" else "")


def can_zip(j):
    return j["status"] == "done" or (j["status"] in ("failed", "cancelled") and bool(j.get("started")))


# ------------------------------------------------------------ the queue --
def status_line(j, now):
    """The badge and the texts under a job card's name."""
    s = j["status"]
    if s == "queued":
        return {"badge": ("queued", f"#{j.get('queue_position') or '?'} in queue"),
                "texts": [f"added {clock(j.get('created'))}"]}
    if s == "running":
        c = j.get("current")
        return {"badge": ("running", "running"), "texts": [current_label(j)],
                # the step's running time; the page's clock makes it tick
                "tick": {"since": c.get("since"), "text": dur(since(c, now))} if c else None,
                "extra": ("cancelled", "cancelling") if j.get("cancel_requested") else None}
    if s == "done":
        return {"badge": ("done", "all checks passed" if j.get("all_passed") else "done · findings left"),
                "texts": [dur((j.get("finished") or 0) - (j.get("started") or 0))]}
    if s == "failed":
        err = ((j.get("error_node") + ": ") if j.get("error_node") else "") + (j.get("error") or "")
        return {"badge": ("failed", "failed"), "texts": [err[:70]], "title": j.get("error") or ""}
    return {"badge": ("cancelled", "cancelled"), "texts": []}


def card(j, now):
    name, stamp = run_name(j)
    return {
        "id": j["id"], "status": j["status"], "name": name, "stamp": stamp,
        "meta": f"{plural(j.get('cq_count') or 0, 'CQ')} · {j.get('model')} · "
                f"reasoning {j.get('reasoning')} · {len(j.get('agents') or [])}/5 agents",
        "line": status_line(j, now),
        "progress": round(progress(j) * 100, 1) if j["status"] == "running" else None,
        "zip": can_zip(j), "active": j["status"] in ACTIVE,
    }


def queue(jobs, now, show_all=False, keep=None, limit=12):
    """The queue in its three sections, and the counts over it. Of the
    finished jobs only the newest `limit` (and the one shown, `keep`) are
    listed unless show_all."""
    running = [card(j, now) for j in jobs if j["status"] == "running"]
    waiting = [card(j, now) for j in sorted((j for j in jobs if j["status"] == "queued"),
                                            key=lambda j: j.get("queue_position") or 0)]
    ended = [j for j in jobs if j["status"] not in ACTIVE]
    shown = ended if show_all else [j for i, j in enumerate(ended) if i < limit or j["id"] == keep]
    count = lambda k: sum(1 for j in jobs if j["status"] == k)
    return {"sections": [(t, c) for t, c in (("Running", running), ("Waiting", waiting),
                                              ("Finished", [card(j, now) for j in shown])) if c],
            "counts": {k: count(k) for k in ("running", "queued", "done", "failed")},
            "active": len(running) + len(waiting),
            "hidden": len(ended) - len(shown), "all": show_all and len(ended) > limit}


# ----------------------------------------------------------- the detail --
def detail(j, now, info=None):
    """Everything the right-hand side shows for one job."""
    s = j["status"]
    name, stamp = run_name(j)
    files = j.get("files") or []
    wall = ((j.get("finished") or now) - j["started"]) if j.get("started") and (j.get("finished") or now) else None
    owl = next((f for f in files if f.endswith("_ontology.owl")), None)
    p = j.get("position") or {}
    if p.get("round"):
        rnd = f"{p['round']} / {p.get('loop_rounds')}" + (" · verify" if p.get("phase") == "verify" else "")
    else:
        rnd = str(j["rounds"]) if j.get("rounds") else "–"

    verdict = None
    if s == "done":
        verdict = (("ok", "✓ ALL CHECKS PASSED", "the final ontology passes every validation tool")
                   if j.get("all_passed") else
                   ("no", "! NOT ALL CHECKS PASSED",
                    f"findings were left after {j.get('rounds')} round(s); the final ontology is still produced"))
    elif s == "failed":
        verdict = ("bad", "✕ Failed" + (f" at {j['error_node']}" if j.get("error_node") else ""), j.get("error") or "")
    elif s == "cancelled":
        verdict = ("no", "Cancelled", j.get("error") or "")
    elif s == "queued":
        verdict = ("q", f"Waiting in the queue · position {j.get('queue_position') or '?'}",
                   "it starts when the jobs before it finish")

    release = None
    if s in ENDED and j.get("model_release"):
        note = j["model_release"]
        release = ("", note + (" · its memory is free for the next job" if note.endswith("released") else ""))
    elif s in ENDED and j.get("started"):
        release = ("wait", f"Releasing {j.get('model')} from Ollama…")

    retries = p.get("tool_retries") or (info or {}).get("tool_retries") or 3
    return {
        "id": j["id"], "status": s, "name": name, "stamp": stamp,
        "sub": " · ".join(x for x in (j.get("cq_source") or "", f"files named {j.get('domain')}_…",
                                      f"queued {when(j.get('created'))}" if j.get("created") else "") if x),
        "zip": bool(files) and s != "queued", "owl": owl if s == "done" else None,
        "active": s in ACTIVE,
        # (label, value, since): a value with `since` is a running time that ticks
        "facts": [("Model", j.get("model"), None), ("Reasoning", j.get("reasoning"), None),
                  ("Questions", j.get("cq_count"), None),
                  ("Running for" if s == "running" else "Duration", dur(wall) if wall is not None else "–",
                   j.get("started") if s == "running" else None),
                  ("Round", rnd, None), ("Corrections", count_fixes(j), None)],
        "verdict": verdict, "release": release,
        "graph": graph_svg(j, now),
        "steps": timeline(j, now, retries),
        "step_count": sum(1 for h in j.get("history") or [] if h.get("node") != "advance"),
        "log": j.get("log") or "(no output yet)",
        "cqs": j.get("cqs") or [],
        "files": files if s != "queued" else [],
        # the release note appears a moment after a job ends: keep refreshing until then
        "settling": s in ENDED and bool(j.get("started")) and not j.get("model_release"),
    }


# -------------------------------------------------------------- the steps --
def step_files(no, files, running=False):
    """A step's input and output files, for the toggle under the step."""
    if not no or not files:
        return None
    inp, out = files.get("input") or [], files.get("output") or []
    if not (inp or out or running):
        return None
    prefix, src = files.get("prefix") or "", files.get("from") or {}

    def item(f):
        short = f[len(prefix) + 1:] if prefix and f.startswith(prefix + "_") else f
        short = re.sub(r"^(input|output)_", "", short)
        origin = src.get(f) or ""
        return {"file": f, "label": short, "from": re.sub(r" \(step \d+\)$", "", origin),
                "title": f + (f"\nthe same file as the output of {origin}" if origin else "")}
    return {"no": no, "n": len(inp) + len(out), "input": [item(f) for f in inp],
            "output": [item(f) for f in out], "out_empty": "when the step ends" if running else "none"}


def timeline(j, now, retries=3):
    """The Steps pane: one row per step, a separator per validation round."""
    rows, last = [], (None, None)
    on = {s["node"] for s in j.get("steps") or [] if s.get("on")}

    def row(ic, sym, title, **kw):
        rows.append(dict(ic=ic, sym=sym, title=title, note=kw.get("note", ""), small=kw.get("small", ""),
                         small_title=kw.get("small_title", ""), du=kw.get("du", ""), bold=kw.get("bold", False),
                         files=kw.get("files"), since=kw.get("since")))

    for h in j.get("history") or []:
        node = h.get("node")
        if node in ("check_tool", "correct_ontology") and (h.get("round"), h.get("phase")) != last:
            rows.append({"sep": f"Round {h.get('round')}"
                                + (" · verification pass" if h.get("phase") == "verify" else "")})
            last = (h.get("round"), h.get("phase"))
        if node == "advance":
            if h.get("done"):
                row("ok" if h.get("all_passed") else "warn", "✓" if h.get("all_passed") else "!",
                    "Every tool passes · loop finished" if h.get("all_passed") else "No rounds left · loop finished")
            continue
        files = step_files(h.get("step"), h.get("files"))
        if h.get("failed"):
            what = h.get("tool") if node == "check_tool" else STEP_LABELS.get(node, node)
            row("bad", "✕", f"{what} {'cancelled' if h.get('cancelled') else 'failed'}",
                du=dur(h.get("seconds")), files=files)
            continue
        if node == "atomize_cqs" and "atomize_cqs" not in on:
            continue          # the pass-through when CQAtomization is off
        if node == "check_tool":
            passed, terr = h.get("passed"), h.get("tool_error")
            report = h.get("report") or ""
            row("ok" if passed else "warn" if terr else "bad", "✓" if passed else "!" if terr else "✕",
                f"{h.get('tool')} {'passed' if passed else 'tool error' if terr else 'failed'}",
                note=f"· check {h['attempt']}" if h.get("phase") != "verify" and h.get("attempt") else "",
                small=report.split("\n")[0] if not passed else "", small_title=report,
                du=dur(h.get("seconds")), files=files)
        elif node == "correct_ontology":
            row("", "↻", f"OntoCorrection fixed {h.get('tool')}", small=f"attempt {h.get('attempt')} of {retries}",
                du=dur(h.get("seconds")), files=files)
        else:
            row("", "✓", STEP_LABELS.get(node, node) + ("" if node == "finalize_terms" else " Agent"),
                du=dur(h.get("seconds")), files=files)
    c = j.get("current")
    if c:
        row("run", "●", current_label(j), small="in progress", du=dur(since(c, now)), bold=True,
            since=c.get("since"), files=step_files(c.get("step"), c.get("files"), running=True))
    if not rows:
        row("mark", "…", "Waiting for the jobs before it" if j["status"] == "queued" else "No steps yet")
    return rows


# --------------------------------------------------- the pipeline graph --
def num(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def node(x, y, w, h, cls, title, sub, icon, tick=None):
    """One box of the graph: an agent, a tool or the final ontology.
    tick = (since, prefix): the second line is a running time that ticks."""
    e, cy = html.escape, y + h / 2
    ic = (f'<circle class="ico" cx="{num(x + 22)}" cy="{num(cy)}" r="11" opacity=".16"/>')
    if icon == "agent":
        ic += (f'<circle class="ico" cx="{num(x + 22)}" cy="{num(cy - 3)}" r="3.4"/>'
               f'<path class="ico" d="M{num(x + 15.5)} {num(cy + 7)} a6.5 5.5 0 0 1 13 0z"/>')
    elif icon == "tool":
        ic += f'<path class="ico" d="M{num(x + 16)} {num(cy + .5)} l4 4 l7.5-8 l-1.6-1.5 l-5.9 6.3 l-2.4-2.4z"/>'
    else:
        ic += f'<path class="ico" d="M{num(x + 17)} {num(cy - 6)} h7 l4 4 v8.5 h-11z"/>'
    tokens = cls.split()
    run = mark = ""
    if "running" in tokens:
        run = (f'<g class="spin"><circle cx="{num(x + w - 18)}" cy="{num(y + 18)}" r="6.5" fill="none" '
               f'stroke="var(--run)" stroke-width="2.2" stroke-dasharray="26 12"/></g>')
    elif "ok" in tokens:
        mark = f'<text x="{num(x + w - 22)}" y="{num(y + 23)}" style="fill:var(--ok);font-size:14px;font-weight:800">✓</text>'
    elif "done" in tokens:
        mark = f'<text x="{num(x + w - 22)}" y="{num(y + 23)}" style="fill:var(--accent);font-size:14px;font-weight:800">✓</text>'
    elif "fail" in tokens:
        mark = f'<text x="{num(x + w - 21)}" y="{num(y + 23)}" style="fill:var(--bad);font-size:13px;font-weight:800">✕</text>'
    elif "warn" in tokens:
        mark = f'<text x="{num(x + w - 18)}" y="{num(y + 23)}" style="fill:var(--warn);font-size:15px;font-weight:800">!</text>'
    box = f'x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" rx="13"'
    return (f'<g class="nd {cls}"><rect class="halo" {box}/><rect class="box" {box}/>{ic}'
            f'<text x="{num(x + 42)}" y="{num(cy - 3)}">{e(title)}</text>'
            f'<text class="s" x="{num(x + 42)}" y="{num(cy + 13)}"'
            + (f' data-since="{tick[0]}" data-prefix="{e(tick[1])}"' if tick and tick[0] else "")
            + f'>{e(sub)}</text>{run}{mark}</g>')


def graph_svg(j, now):
    """The live pipeline: the requirements agents in a column, the MCP
    validation tools on a ring around OntoCorrection, then the ontology."""
    hist, cur = j.get("history") or [], j.get("current")
    ok = [h for h in hist if not h.get("failed")]
    seen = {h.get("node") for h in ok}
    failed_at = {h.get("node") for h in hist if h.get("failed")}
    secs = lambda n: sum(h.get("seconds") or 0 for h in hist if h.get("node") == n)
    running_for = dur(since(cur, now)) if cur else ""
    cur_since = cur.get("since") if cur else None
    out, edges = [], []
    ended = j["status"] in ("failed", "cancelled")
    steps = j.get("steps") or []

    # the requirements column
    AX, AW, AH, AG, AY = 20, 258, 50, 64, 46
    out.append(f'<text class="lbl" x="{AX}" y="{AY - 16}">Requirements phase</text>')
    for i, s in enumerate(steps):
        y = AY + i * AG
        cls, sub = "pending", "waiting"
        if not s.get("on"):
            cls, sub = "off", "switched off"
        elif cur and cur.get("node") == s["node"]:
            cls, sub = "running", "running · " + running_for
        elif s["node"] in failed_at:
            cls, sub = "fail", "failed"
        elif s["node"] in seen:
            cls, sub = "done", "done · " + dur(secs(s["node"]))
        elif ended:
            sub = "not reached"
        elif j["status"] == "queued":
            sub = "queued"
        out.append(node(AX, y, AW, AH, cls, s.get("label") or s["node"], sub, "agent",
                        (cur_since, "running · ") if cls == "running" else None))
        if i:
            prev = steps[i - 1]
            ec = ("active" if cls == "running" else
                  "done" if s["node"] in seen or (not s.get("on") and prev["node"] in seen) else
                  "off" if not s.get("on") else "")
            edges.append(f'<path class="ed {ec}" d="M{num(AX + AW / 2)} {num(y - AG + AH)} L{num(AX + AW / 2)} {num(y)}"/>')

    # the validation ring (an ellipse: the tools are wide)
    CX, CY, RX, RY, TW, TH = 656, 262, 236, 170, 200, 52
    tools = j.get("tools") or []
    n = len(tools) or 1
    a0 = math.pi * 1.25 if n == 4 else math.pi
    ang = lambda i: a0 + i * 2 * math.pi / n
    P = lambda a, k=1: (CX + RX * k * math.cos(a), CY + RY * k * math.sin(a))
    in_loop = bool(cur) and cur.get("node") in ("check_tool", "correct_ontology", "advance")
    loop_started = "generate_ontology" in seen
    out.append(f'<text class="lbl" x="{CX}" y="{AY - 16}" text-anchor="middle">Validation loop · MCP tools</text>')
    edges.append(f'<ellipse class="ed {"ring-run" if in_loop else "done" if loop_started else ""}" cx="{CX}" cy="{CY}" '
                 f'rx="{RX}" ry="{RY}" style="{"opacity:.45" if loop_started and not in_loop else ""}"/>')
    for i in range(len(tools)):         # clockwise arrowheads half-way between two tools
        a = (ang(i) + ang(i + 1)) / 2
        x, y = P(a)
        tx, ty = -RX * math.sin(a), RY * math.cos(a)
        L = math.hypot(tx, ty)
        tx, ty = tx / L, ty / L
        s2, nx, ny = 7, -ty, tx
        edges.append(f'<path class="arrow {"on" if loop_started else ""}" d="M{num(x + tx * s2)} {num(y + ty * s2)} '
                     f'L{num(x - tx * s2 + nx * s2 * .8)} {num(y - ty * s2 + ny * s2 * .8)} '
                     f'L{num(x - tx * s2 - nx * s2 * .8)} {num(y - ty * s2 - ny * s2 * .8)}z"/>')

    # OntoCorrection in the centre, a spoke to every tool it fixed
    corr = [h for h in ok if h.get("node") == "correct_ontology"]
    fixing = cur.get("tool") if cur and cur.get("node") == "correct_ontology" else None
    for i, t in enumerate(tools):
        if not any(h.get("tool") == t for h in corr) and fixing != t:
            continue
        (x, y), (x2, y2) = P(ang(i), .8), P(ang(i), .24)
        edges.append(f'<path class="ed {"active" if fixing == t else "done"}" d="M{num(x)} {num(y)} L{num(x2)} {num(y2)}" '
                     f'style="{"" if fixing == t else "opacity:.45"}"/>')
    ccls = "done" if corr else "pending"
    csub = plural(len(corr), "correction") if corr else "fixes what a tool reports"
    if fixing:
        ccls, csub = "running", f"fixing {fixing} · {running_for}"
    if not loop_started and not fixing:
        csub = "waits for the first check"
    out.append(node(CX - 100, CY - 28, 200, 56, ccls, "OntoCorrection", csub, "agent",
                    (cur_since, f"fixing {fixing} · ") if fixing else None))

    # the round pill
    p, pill = j.get("position") or {}, ""
    if p.get("round"):
        pill = f"round {p['round']}/{p.get('loop_rounds')} · " + ("verification pass" if p.get("phase") == "verify"
                                                               else "check & correct")
    elif j.get("rounds"):
        pill = plural(j["rounds"], "round")
    if pill:
        w = len(pill) * 6.5 + 26
        out.append(f'<g><rect class="pill-bg" x="{num(CX - w / 2)}" y="{CY + 40}" width="{num(w)}" height="24" rx="12"/>'
                   f'<text class="pill-tx" x="{CX}" y="{num(CY + 56.5)}" text-anchor="middle">{html.escape(pill)}</text></g>')

    # the tools on the ring
    for i, t in enumerate(tools):
        checks = [h for h in ok if h.get("node") == "check_tool" and h.get("tool") == t]
        fixes = sum(1 for h in corr if h.get("tool") == t)
        last = checks[-1] if checks else None
        cls, sub = "pending", "not checked yet" if loop_started else "waiting"
        if cur and cur.get("node") == "check_tool" and cur.get("tool") == t:
            cls, sub = "running", "checking · " + running_for
        elif fixing == t:
            cls, sub = "fail running", "being fixed"
        elif last:
            cls = "ok" if last.get("passed") else "warn" if last.get("tool_error") else "fail"
            sub = (("passed" if last.get("passed") else "tool error" if last.get("tool_error") else "failed")
                   + f" · {len(checks)}×" + (f" · {fixes} fix{'es' if fixes > 1 else ''}" if fixes else ""))
        x, y = P(ang(i))
        out.append(node(x - TW / 2, y - TH / 2, TW, TH, cls, t, sub, "tool",
                        (cur_since, "checking · ") if cls == "running" else None))

    # OntoGeneration -> the ring, the ring -> the final ontology
    gy = AY + (len(steps) - 1) * AG + AH / 2
    sx, sy = P(ang(0))
    first_check = bool(cur) and cur.get("node") == "check_tool" and not any(h.get("node") == "check_tool" for h in hist)
    edges.append(f'<path class="ed {"active" if first_check else "done" if loop_started else ""}" '
                 f'd="M{AX + AW} {num(gy)} C{AX + AW + 60} {num(gy)} {num(sx - TW / 2 - 60)} {num(sy)} {num(sx - TW / 2)} {num(sy)}"/>')
    FX, FW = CX + RX + 66, 150
    fcls, fsub = "pending", "final ontology"
    if j["status"] == "done":
        fcls, fsub = ("ok", "all checks passed") if j.get("all_passed") else ("warn", "findings left")
    elif j["status"] == "failed":
        fcls, fsub = "fail", "run failed"
    elif j["status"] == "cancelled":
        fcls, fsub = "off", "cancelled"
    edges.append(f'<path class="ed {"done" if j["status"] == "done" else ""}" d="M{CX + RX} {CY} L{FX} {CY}"/>')
    out.append(f'<text class="lbl" x="{CX + RX + 8}" y="{CY - 9}">finish</text>')
    out.append(node(FX, CY - 28, FW, 56, fcls, "Ontology", fsub, "end"))
    H = max(AY + len(steps) * AG, CY + RY + TH / 2 + 12)
    return Markup(f'<svg class="graph" viewBox="0 0 {FX + FW + 16} {num(H)}" role="img" '
                  f'aria-label="pipeline progress">{"".join(edges)}{"".join(out)}</svg>')


# ------------------------------------------------------------- the form --
def model_kind(m):
    """How the reasoning control looks for a model (CSS picks it up)."""
    if m.get("thinking") is False:
        return "no"
    if m.get("levels"):
        return "levels"
    return "onoff" if m.get("thinking") else "plain"


def form_models(info):
    """The Model list of the form, with what each model can do with reasoning."""
    models = (info or {}).get("models") or [{"name": n, "thinking": None, "levels": []}
                                            for n in (info or {}).get("ollama_models") or []]
    if not models:
        models = [{"name": (info or {}).get("default_model") or "qwen3.6:27b", "thinking": None, "levels": []}]
    return [{"name": m["name"], "kind": model_kind(m), "levels": " ".join(m.get("levels") or [])}
            for m in models]


def count_cqs(text):
    """About how many questions a text or file holds (for the form's hints;
    the service parses them for real)."""
    text = (text or "").strip().lstrip("﻿")
    if not text:
        return 0
    if text[0] in "[{":
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list)), [])
            if isinstance(data, list):
                return sum(1 for c in data if c and (isinstance(c, str) or isinstance(c, dict) and (
                    c.get("value") or c.get("question") or c.get("cq") or c.get("text"))))
        except ValueError:
            pass
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and "," in lines[0] and re.search(r"\b(value|question|cq)\b", lines[0], re.I):
        return len(lines) - 1
    return len(lines)


def reasoning_value(model, reason_on, effort, info):
    """What the form's Reasoning switch and Effort mean for the model (the
    service applies the same rule again)."""
    cap = next((m for m in (info or {}).get("models") or [] if m.get("name") == model), {})
    if not reason_on or cap.get("thinking") is False:
        return "off"
    levels = cap.get("levels") or []
    if levels:
        return effort if effort in levels else ("medium" if "medium" in levels else levels[0])
    return "on"
