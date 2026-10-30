"""Generate docs/hld.excalidraw: the Unicorn Scout high-level design, in a hand-drawn Excalidraw style.

Usage: python docs/make_hld.py   (writes docs/hld.excalidraw and docs/hld.clipboard.json for pasting)
"""
import json
import random
from pathlib import Path

INK, BLUE, RED, GREEN, ORANGE, GREY = "#1e1e1e", "#1971c2", "#e03131", "#2f9e44", "#f08c00", "#868e96"
FONT = 5  # Excalifont
els: list[dict] = []
_n = 0


def _id(prefix: str) -> str:
    global _n
    _n += 1
    return f"{prefix}{_n:03d}"


def _base(kind: str, x, y, w, h, color, **kw) -> dict:
    return {"id": _id(kind[:3]), "type": kind, "x": x, "y": y, "width": w, "height": h, "angle": 0,
            "strokeColor": color, "backgroundColor": "transparent", "fillStyle": "solid", "strokeWidth": 2,
            "strokeStyle": kw.pop("style", "solid"), "roughness": kw.pop("rough", 0), "opacity": 100,
            "groupIds": [], "frameId": None, "roundness": kw.pop("roundness", None),
            "seed": random.randint(1, 2**31), "version": 1, "versionNonce": random.randint(1, 2**31),
            "isDeleted": False, "boundElements": [], "updated": 1, "link": None, "locked": False, **kw}


def _measure(text: str, size: float, k: float = 0.55) -> tuple[float, float]:
    lines = text.split("\n")
    return max(len(line) for line in lines) * size * k, len(lines) * size * 1.25


def text(x, y, s, color=INK, size=20, align="left") -> dict:
    w, h = _measure(s, size, 0.62)  # free text: generous width so nothing clips
    e = _base("text", x, y, w, h, color, text=s, originalText=s, fontSize=size, fontFamily=FONT,
              textAlign=align, verticalAlign="top", containerId=None, autoResize=True, lineHeight=1.25)
    els.append(e)
    return e


def box(x, y, label, color=INK, w=0, h=0, size=20, text_color=None, style="solid", rough=0, shape="rectangle"):
    tw, th = _measure(label, size)
    w, h = max(w, tw + 36), max(h, th + 28)
    b = _base(shape, x, y, w, h, color, style=style, rough=rough,
              roundness={"type": 3} if shape == "rectangle" else {"type": 2})
    t = _base("text", x + (w - tw) / 2, y + (h - th) / 2, tw, th, text_color or color, text=label,
              originalText=label, fontSize=size, fontFamily=FONT, textAlign="center", verticalAlign="middle",
              containerId=b["id"], autoResize=True, lineHeight=1.25)
    b["boundElements"].append({"type": "text", "id": t["id"]})
    els.extend([b, t])
    return b


def L(b): return (b["x"], b["y"] + b["height"] / 2)
def R(b): return (b["x"] + b["width"], b["y"] + b["height"] / 2)
def T(b): return (b["x"] + b["width"] / 2, b["y"])
def B(b): return (b["x"] + b["width"] / 2, b["y"] + b["height"])


def arrow(p0, p1, color=INK, via=None, start=None, end=None, style="solid", head="arrow", rough=0):
    pts = [p0] + (via or []) + [p1]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    a = _base("arrow", p0[0], p0[1], max(xs) - min(xs), max(ys) - min(ys), color, style=style, rough=rough,
              roundness={"type": 2}, points=[[px - p0[0], py - p0[1]] for px, py in pts], lastCommittedPoint=None,
              startBinding=None, endBinding=None, startArrowhead=None, endArrowhead=head, elbowed=False)
    for key, target in (("startBinding", start), ("endBinding", end)):
        if target:
            a[key] = {"elementId": target["id"], "focus": 0, "gap": 6}
            target["boundElements"].append({"type": "arrow", "id": a["id"]})
    els.append(a)
    return a


def line(p0, p1, color=INK):
    a = _base("line", p0[0], p0[1], abs(p1[0] - p0[0]), abs(p1[1] - p0[1]), color, roundness={"type": 2},
              points=[[0, 0], [p1[0] - p0[0], p1[1] - p0[1]]], lastCommittedPoint=None, startBinding=None,
              endBinding=None, startArrowhead=None, endArrowhead=None)
    els.append(a)


random.seed(7)

# ---------------------------------------------------------------- title
text(1130, 20, "Unicorn Scout · how a question flows", BLUE, 40)
text(1010, 78, "GenAI chatbot over 118 Indian unicorns (Tracxn, May 2025) · FastAPI on Vercel · Groq / Gemini · Neon pgvector · Langfuse", GREY, 18)

# ---------------------------------------------------------------- main flow (left -> right)
Y = 540
user = box(40, Y, "User asks\na question", BLUE, w=200)
text(30, 440, "signed in\nwith Google", GREEN, 18)

guard = box(330, Y, "Input guardrails\n(sanitize.py)", w=240)
text(320, 425, "trim · drop invisible chars\n≤ 500 characters\n'ignore previous…' regex", GREEN, 18)

pg = box(660, Y, "Prompt Guard 2\n(Llama 86M · Groq)", w=250)
text(670, 435, "normal question ≈ 0.0004\nscore ≥ 0.8 → blocked", GREEN, 18)

mem = box(1000, Y, "Memory\n(chat_logs · Neon)", w=240)
text(990, 425, "last 3 turns +\ncompanies from the last answer\n= what \"these\" means", GREEN, 18)

router = box(1330, Y - 10, "Router LLM\n(gpt-oss-120b → JSON)", ORANGE, w=280, h=100)
text(1330, 650, "intent · filters · company names\nfollow-up? · search query\nprompt: router v4 (Langfuse)", ORANGE, 18)

arrow(R(user), L(guard), start=user, end=guard)
arrow(R(guard), L(pg), start=guard, end=pg)
arrow(R(pg), L(mem), start=pg, end=mem)
arrow(R(mem), L(router), start=mem, end=router)

# failure branches (red, below)
locked = box(30, 730, "Not signed in →\n401 + locked chat box", RED, w=230)
arrow(B(user), T(locked), RED, start=user, end=locked)
blocked = box(470, 740, "Rejected / blocked\npolite reply · still logged", RED, w=280)
arrow(B(guard), (560, 740), RED, start=guard, end=blocked)
arrow(B(pg), (680, 740), RED, start=pg, end=blocked)
text(268, 655, "empty · junk\ntoo long · injection", RED, 16)
text(800, 655, "score ≥ 0.8", RED, 16)

# ---------------------------------------------------------------- router fan-out: one path per intent
CX = 1720
clarify = box(CX, 250, "vague → clarify\nask one question +\nclickable options", ORANGE, w=300)
greet = box(CX, 385, "greeting → hello + examples", GREY, w=300, text_color=INK)
offt = box(CX, 465, "off-topic → polite decline", RED, w=300)
lookup = box(CX, 560, "company lookup\nexact name match\n(typo fallback)", w=300)
flt = box(CX, 685, "filter / list\nSQL: sector group · city alias\nstage · year · investor\nscoped to \"these\" on follow-ups", w=300)
rec = box(CX, 845, "needs-based match\npgvector similarity\n768-d gemini-embedding-001", w=300)
BUS = 1665
line((R(router)[0], R(router)[1]), (BUS, R(router)[1]))
line((BUS, L(clarify)[1]), (BUS, L(rec)[1]))
for b in (clarify, greet, offt, lookup, flt, rec):
    arrow((BUS, L(b)[1]), L(b), end=b)
text(CX + 2, 965, "the router's intent picks one path", ORANGE, 18)
text(CX + 350, 400, "reply straight away\n(no search)", GREEN, 18)

# clarify loops back to the user (like a 'revise' loop)
arrow(T(clarify), T(user), GREEN, via=[(520, 190)], start=clarify, end=user, rough=1)
text(420, 120, "user clicks an option →\nmerged with the original question", GREEN, 18)

# ---------------------------------------------------------------- retrieve -> answer -> output
rows = box(2140, 680, "Retrieved companies\n(only these rows)", w=270)
for b in (lookup, flt, rec):
    arrow(R(b), L(rows), start=b, end=rows)
text(2145, 790, "0 rows → \"no match\" reply\n(never invents companies)", GREEN, 18)

answer = box(2500, 680, "Answer LLM\n(gpt-oss-120b · streamed)", w=290)
text(2490, 560, "grounded only in those rows\n+ totals computed in code\nprompt: answer v5 (Langfuse)", GREEN, 18)
arrow(R(rows), L(answer), start=rows, end=answer)

out = text(2880, 676, "OUTPUT", GREEN, 44)
out["width"] = 6 * 44 * 0.8  # Excalifont capitals run wide
text(2858, 740, "table · fact cards · picks\n+ streamed answer text", GREEN, 18)
arrow(R(answer), (2872, 704), GREEN)

# ---------------------------------------------------------------- LLM fallback chain (every LLM call)
fb = box(2130, 170, "", GREY, w=1000, h=210, style="dashed")
text(2150, 182, "LLM fallback chain · used by the router and the answer", RED, 18)
groq = box(2160, 225, "Groq\ngpt-oss-120b", w=230)
g31 = box(2500, 225, "Gemini\n3.1 Flash-Lite", w=230)
g35 = box(2840, 225, "Gemini\n3.5 Flash-Lite", w=230)
arrow(R(groq), L(g31), RED, start=groq, end=g31)
arrow(R(g31), L(g35), RED, start=g31, end=g35)
text(2160, 320, "429 · 5xx · 12 s timeout → next   ·   each provider rotates 2 API keys first", RED, 16)
text(2160, 345, "with Groq down, answers still arrive in 3-10 s", GREEN, 16)

# ---------------------------------------------------------------- data ingest + storage + observability (bottom)
text(30, 1040, "Offline · runs once (scripts/ingest.py)", RED, 20)
csv = box(30, 1090, "tracxn.csv\n118 rows × 19 cols", BLUE, w=220)
clean = box(320, 1080, "Clean\ncp1252 · ₹ restored · cities\nshifted columns dropped\n56 sectors → 12 groups", w=290)
embed = box(680, 1090, "Embed each company\nname + sector + city +\ndescription → 768 numbers", w=300)
neon = box(1060, 1070, "Neon Postgres + pgvector\ncompanies (118 + embeddings)\nchat_logs (1 row per turn)\napp_logs · users · chat_sessions", BLUE, w=380)
lf = box(1530, 1080, "Langfuse\ntraces (step tree) · sessions\nusers · prompt versions v1→v5\noutcome scores", ORANGE, w=360)
arrow(R(csv), L(clean), start=csv, end=clean)
arrow(R(clean), L(embed), start=clean, end=embed)
arrow(R(embed), L(neon), start=embed, end=neon)

arrow(B(mem), (1150, 1070), GREY, style="dashed", start=mem, end=neon)
text(1160, 900, "read history", GREY, 16)
arrow(B(answer), (1890, 1150), GREEN, via=[(2400, 1100)], style="dashed", start=answer, end=lf, rough=1)
text(2050, 1150, "log every turn: chat_logs row\n+ Langfuse trace & outcome score", GREEN, 18)

# ---------------------------------------------------------------- write
scene = {"type": "excalidraw", "version": 2, "source": "https://excalidraw.com", "elements": els,
         "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None}, "files": {}}
here = Path(__file__).parent
(here / "hld.excalidraw").write_text(json.dumps(scene, ensure_ascii=False, indent=1), encoding="utf-8")
(here / "hld.clipboard.json").write_text(json.dumps({"type": "excalidraw/clipboard", "elements": els, "files": {}},
                                                    ensure_ascii=False), encoding="utf-8")
print(f"{len(els)} elements -> docs/hld.excalidraw")
