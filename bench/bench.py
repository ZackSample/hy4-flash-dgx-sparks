#!/usr/bin/env python3
"""Speed + ability benchmark for a local OpenAI-compatible chat server.

Standard library only.  Typical use:

  python3 bench.py --base http://localhost:8888/v1 --model hy4-flash --label hy4 \
      --out results/hy4.json

  python3 bench.py --selftest        # check graders, PNGs and the docker sandbox (no server needed)

Every request is streamed with stream_options.include_usage.  Reasoning deltas
(`reasoning_content` or `reasoning`) and content deltas are counted separately.
Errors are recorded per test and never stop the run.  Results are written to
--out (JSON, rewritten after every test) and <out dir>/<label>.md.
"""

import argparse
import base64
import datetime
import json
import math
import os
import random
import re
import shutil
import statistics
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import zlib
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX_IMAGE = "vllm/vllm-openai:hy4-flash"

DEFAULT_THINK_OFF = {"reasoning_effort": "no_think"}
DEFAULT_THINK_ON = {"reasoning_effort": "high"}


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

def deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def median(xs):
    xs = [x for x in xs if isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x))]
    return statistics.median(xs) if xs else None


def fmt(x, nd=2, unit=""):
    if x is None:
        return "-"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, float):
        return f"{x:.{nd}f}{unit}"
    return f"{x}{unit}"


def strip_think(text):
    """Remove inline <think>...</think> blocks (servers without a reasoning parser)."""
    if not text:
        return ""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


def short(s, n=160):
    s = "" if s is None else str(s)
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[: n - 3] + "..."


def summarize_body(body, limit=2000):
    """Copy of a request body that is safe to store (huge strings and images trimmed)."""
    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, str):
            if o.startswith("data:image"):
                return f"<data url, {len(o)} chars>"
            if len(o) > limit:
                return o[: limit // 2] + f" ...<{len(o) - limit} chars cut>... " + o[-limit // 2:]
        return o
    return walk(body)


# ----------------------------------------------------------------------------
# streaming client
# ----------------------------------------------------------------------------

class Client:
    def __init__(self, base, model, timeout=900.0, api_key=None):
        self.base = base.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.api_key = api_key

    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        return h

    def get_models(self):
        try:
            req = urllib.request.Request(self.base + "/models", headers=self._headers())
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    def chat(self, body, timeout=None):
        """Stream one chat completion. Never raises; returns a result dict."""
        timeout = timeout or self.timeout
        body = dict(body)
        body.setdefault("model", self.model)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        r = {
            "ok": False, "error": None, "http_status": None,
            "ttft": None, "t_first_content": None, "total_time": None, "decode_time": None,
            "prompt_tokens": None, "completion_tokens": None, "reasoning_tokens": None,
            "answer_tokens": None, "token_source": None, "split_source": None,
            "tps": None, "prompt_tps": None,
            "content": "", "reasoning": "", "tool_calls": [], "finish_reason": None,
            "n_reasoning_deltas": 0, "n_content_deltas": 0, "n_tool_deltas": 0,
            "usage": None, "bad_chunks": 0,
        }
        content_parts, reasoning_parts = [], []
        tools = {}
        t0 = time.perf_counter()
        r["_t0"] = t0
        t_first = t_last = t_first_content = None
        done = False
        try:
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(self.base + "/chat/completions", data=data,
                                         headers=self._headers(), method="POST")
            resp = urllib.request.urlopen(req, timeout=timeout)
            r["http_status"] = resp.status
            ctype = resp.headers.get("Content-Type", "") or ""
            if "event-stream" not in ctype and "json" in ctype:
                # Server ignored stream=True: parse a whole response.
                obj = json.loads(resp.read().decode("utf-8", "replace"))
                now = time.perf_counter()
                t_first = t_last = t_first_content = now
                r["usage"] = obj.get("usage")
                for ch in obj.get("choices") or []:
                    msg = ch.get("message") or {}
                    content_parts.append(msg.get("content") or "")
                    reasoning_parts.append(msg.get("reasoning_content") or msg.get("reasoning") or "")
                    for i, tc in enumerate(msg.get("tool_calls") or []):
                        fn = tc.get("function") or {}
                        tools[i] = {"id": tc.get("id"), "name": fn.get("name") or "",
                                    "arguments": fn.get("arguments") or ""}
                    r["finish_reason"] = ch.get("finish_reason")
                r["warning"] = "server returned a non-streaming response; timings are not per-token"
            else:
                for raw in resp:
                    now = time.perf_counter()
                    if now - t0 > timeout:
                        raise TimeoutError(f"total time exceeded {timeout}s")
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        done = True
                        break
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        r["bad_chunks"] += 1
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if obj.get("error"):
                        r["error"] = "stream error: " + short(json.dumps(obj["error"]), 600)
                        break
                    if obj.get("usage"):
                        r["usage"] = obj["usage"]
                    for ch in obj.get("choices") or []:
                        d = ch.get("delta") or {}
                        got = False
                        rc = d.get("reasoning_content") or d.get("reasoning")
                        if rc:
                            reasoning_parts.append(rc)
                            r["n_reasoning_deltas"] += 1
                            got = True
                        c = d.get("content")
                        if c:
                            content_parts.append(c)
                            r["n_content_deltas"] += 1
                            got = True
                            if t_first_content is None:
                                t_first_content = now
                        for tc in d.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = tools.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                a = fn["arguments"]
                                slot["arguments"] += a if isinstance(a, str) else json.dumps(a)
                            r["n_tool_deltas"] += 1
                            got = True
                            if t_first_content is None:
                                t_first_content = now
                        if got:
                            if t_first is None:
                                t_first = now
                            t_last = now
                        if ch.get("finish_reason"):
                            r["finish_reason"] = ch["finish_reason"]
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
            if r["error"] is None and "event-stream" in ctype and not done:
                if r["finish_reason"]:
                    r["warning"] = "stream ended without [DONE]"
                else:
                    r["error"] = "stream ended early (no [DONE] and no finish_reason); partial output kept"
            if r["error"] is None:
                r["ok"] = True
        except urllib.error.HTTPError as e:
            r["http_status"] = e.code
            try:
                txt = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                txt = ""
            r["error"] = f"HTTP {e.code}: {short(txt, 800)}"
        except urllib.error.URLError as e:
            r["error"] = f"URLError: {e.reason}"
        except (TimeoutError, OSError) as e:
            r["error"] = f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001
            r["error"] = f"{type(e).__name__}: {e}"
        t_end = time.perf_counter()

        r["content"] = "".join(content_parts)
        r["reasoning"] = "".join(reasoning_parts)
        r["tool_calls"] = [tools[k] for k in sorted(tools)]
        r["total_time"] = t_end - t0
        r["_tfirst_abs"] = t_first
        r["_tlast_abs"] = t_last
        if t_first is not None:
            r["ttft"] = t_first - t0
            r["decode_time"] = (t_last - t_first) if t_last else 0.0
        if t_first_content is not None:
            r["t_first_content"] = t_first_content - t0

        usage = r["usage"] or {}
        ct = usage.get("completion_tokens")
        pt = usage.get("prompt_tokens")
        if ct is None:
            ct = r["n_reasoning_deltas"] + r["n_content_deltas"] + (1 if r["n_tool_deltas"] else 0)
            r["token_source"] = "delta count (usage missing)"
        else:
            r["token_source"] = "usage"
        r["completion_tokens"] = ct
        r["prompt_tokens"] = pt
        details = usage.get("completion_tokens_details") or {}
        rt = details.get("reasoning_tokens") if isinstance(details, dict) else None
        inline = not r["reasoning"] and "</think>" in r["content"]
        if inline:
            r["inline_think"] = True
        if rt is not None:
            r["reasoning_tokens"] = rt
            r["answer_tokens"] = max(0, ct - rt)
            r["split_source"] = "usage"
        elif r["token_source"] == "usage" or inline:
            # Split usage tokens in proportion to characters (robust to multi-token deltas).
            rc_chars = len(r["reasoning"]) or (len(r["content"].rsplit("</think>", 1)[0]) if inline else 0)
            ac_chars = len(r["content"]) - (rc_chars if inline else 0) + sum(len(t["arguments"]) + len(t["name"]) for t in r["tool_calls"])
            if rc_chars + ac_chars == 0:
                r["reasoning_tokens"], r["answer_tokens"] = 0, ct
            else:
                r["reasoning_tokens"] = round(ct * rc_chars / (rc_chars + ac_chars))
                r["answer_tokens"] = ct - r["reasoning_tokens"]
            r["split_source"] = r["token_source"] + ", split by characters" + (" (inline <think>)" if inline else "")
        else:
            r["reasoning_tokens"] = r["n_reasoning_deltas"]
            r["answer_tokens"] = ct - r["n_reasoning_deltas"]
            r["split_source"] = "delta count"
        if r["decode_time"] and r["decode_time"] > 0 and ct and ct > 1:
            r["tps"] = (ct - 1) / r["decode_time"]
        if pt and r["ttft"]:
            r["prompt_tps"] = pt / r["ttft"]
        return r


def public(r):
    """Result dict without private timing keys."""
    return {k: v for k, v in r.items() if not k.startswith("_")}


# ----------------------------------------------------------------------------
# synthetic text
# ----------------------------------------------------------------------------

SYLLABLES = ("ba ko ri tel mon sa vu dren li or pa nek ta quo ser fi lum ga dor ex mi ran to zel "
             "ca ven hu pli ost ner bri sto fal gen mur wex jo kla ted nim ral sur vos tur pen ash").split()
COMMON = ("the of and to in is that for it as with was on be by at this from or have an they which one "
          "you were all we her she there would their will when who him been has more if no out so what "
          "up said about other into than its time only could new them man some these then two first may "
          "any like now my such make over our even most me state after also made many did must before "
          "back see through way where get much go well your know should down work year because come "
          "people just day river stone market winter garden light old north small long road house").split()
ADJ = ("COBALT AMBER JASPER SILVER CRIMSON VIOLET COPPER SAFFRON INDIGO TEAL OCHRE IVORY "
       "MAROON CITRINE ONYX TOPAZ").split()


def word_like(rng):
    if rng.random() < 0.55:
        return rng.choice(COMMON)
    return "".join(rng.choice(SYLLABLES) for _ in range(rng.randint(1, 3)))


def gen_text(rng, n_chars):
    out, size = [], 0
    while size < n_chars:
        words = [word_like(rng) for _ in range(rng.randint(7, 16))]
        s = " ".join(words).capitalize() + rng.choice([".", ".", ".", "?", "!"])
        if rng.random() < 0.12:
            s += "\n\n"
        else:
            s += " "
        out.append(s)
        size += len(s)
    return "".join(out)


FIRST = "Velmor Asha Bren Tovi Kesh Liora Mardo Nerys Orrin Pell Quill Rusa Sefa Tamsin Ulric Wren".split()
LAST = "Dask Ombry Venn Halloway Kestrel Morrow Pike Quarry Rook Sallow Thorne Vale Wick Yarrow".split()
DISTRICT = "Ostrin Belcourt Harrow Lindmere Quayside Redfern Saltmarsh Tolbridge Upmoor Westvale".split()
GOODS = "parcels crates letters barrels sacks bundles".split()


def gen_records(rng, n_chars, start=1):
    out, size, i = [], 0, start
    while size < n_chars:
        s = (f"Record {i:05d}: courier {rng.choice(FIRST)} {rng.choice(LAST)} of {rng.choice(DISTRICT)} "
             f"district delivered {rng.randint(3, 480)} {rng.choice(GOODS)} on day {rng.randint(1, 365)}, "
             f"route {rng.randint(10, 99)}, rating {rng.randint(1, 5)} of 5.\n")
        out.append(s)
        size += len(s)
        i += 1
    return out


# ----------------------------------------------------------------------------
# PNG generation (pure python)
# ----------------------------------------------------------------------------

class Canvas:
    def __init__(self, w, h, bg=(255, 255, 255)):
        self.w, self.h = w, h
        self.px = bytearray(bytes(bg) * (w * h))

    def rect(self, x0, y0, x1, y1, col):
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.w, x1), min(self.h, y1)
        row = bytes(col) * (x1 - x0)
        for y in range(y0, y1):
            i = (y * self.w + x0) * 3
            self.px[i:i + len(row)] = row

    def circle(self, cx, cy, r, col):
        for y in range(max(0, cy - r), min(self.h, cy + r + 1)):
            dy = y - cy
            dx = int(math.sqrt(max(0, r * r - dy * dy)))
            self.rect(cx - dx, y, cx + dx + 1, y + 1, col)

    def digit(self, d, x, y, scale, col):
        font = {1: ["010", "110", "010", "010", "111"], 2: ["111", "001", "111", "100", "111"],
                3: ["111", "001", "111", "001", "111"], 4: ["101", "101", "111", "001", "001"]}
        for ry, row in enumerate(font[d]):
            for rx, bit in enumerate(row):
                if bit == "1":
                    self.rect(x + rx * scale, y + ry * scale, x + (rx + 1) * scale, y + (ry + 1) * scale, col)

    def png(self):
        stride = self.w * 3
        raw = b"".join(b"\x00" + bytes(self.px[y * stride:(y + 1) * stride]) for y in range(self.h))

        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


RED, BLUE, GREEN, YELLOW, BLACK, GREY = (220, 30, 30), (30, 60, 220), (30, 170, 60), (240, 210, 30), (0, 0, 0), (90, 90, 90)


def img_shapes():
    c = Canvas(512, 512)
    for x, y, s in [(40, 40, 110), (300, 60, 90), (180, 330, 100)]:
        c.rect(x, y, x + s, y + s, RED)
    c.circle(140, 230, 50, BLUE)
    c.circle(400, 380, 55, BLUE)
    return c.png()


BAR_HEIGHTS = [140, 230, 380, 260]


def img_bars():
    c = Canvas(512, 512)
    base = 440
    c.rect(50, 40, 54, base + 2, BLACK)       # y axis
    c.rect(50, base, 490, base + 4, BLACK)    # x axis
    for i, h in enumerate(BAR_HEIGHTS):
        x = 80 + i * 105
        c.rect(x, base - h, x + 70, base, (40, 100, 200))
        c.digit(i + 1, x + 23, base + 16, 8, BLACK)
    return c.png()


def img_quadrants():
    c = Canvas(512, 512)
    c.rect(0, 0, 256, 256, RED)
    c.rect(256, 0, 512, 256, GREEN)
    c.rect(0, 256, 256, 512, BLUE)
    c.rect(256, 256, 512, 512, YELLOW)
    return c.png()


def data_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# ----------------------------------------------------------------------------
# answer extraction / grading
# ----------------------------------------------------------------------------

NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?(?:\s*/\s*\d+)?|-?\.\d+")


def answer_line(text):
    text = strip_think(text)
    found = None
    for line in text.splitlines():
        clean = line.replace("*", "").replace("`", "").strip()
        m = re.search(r"\bANSWER\s*[:：]\s*(.*)$", clean, re.IGNORECASE)
        if m:
            found = m.group(1)
    if found is None:
        return None
    found = re.sub(r"\\boxed\{(.*)\}", r"\1", found)
    found = found.replace("$", "").replace("\\(", "").replace("\\)", "")
    return found.strip()


def norm_str(s):
    s = (s or "").strip().strip("\"'“”‘’").strip()
    s = re.sub(r"[\s]+", " ", s)
    s = s.rstrip(".!;,").strip().strip("\"'“”‘’").strip()
    return s.lower()


def parse_num(s):
    if s is None:
        return None
    m = NUM_RE.search(s.replace("\u2212", "-"))
    if not m:
        return None
    tok = m.group(0).replace(",", "").replace(" ", "")
    try:
        return Fraction(tok)
    except (ValueError, ZeroDivisionError):
        return None


def grade_answer(kind, expected, text):
    """Returns (passed, got)."""
    got = answer_line(text)
    if got is None:
        return False, None
    if kind == "num":
        v = parse_num(got)
        if v is None:
            return False, got
        e = Fraction(expected)
        return (v == e or abs(float(v) - float(e)) <= 1e-3 * max(1.0, abs(float(e)))), got
    if kind == "str":
        return norm_str(got) == norm_str(expected), got
    if kind == "date":
        m = re.search(r"\d{4}-\d{2}-\d{2}", got)
        return bool(m and m.group(0) == expected), got
    if kind == "list":
        parts = [re.sub(r"^(house\s*)?\d+\s*[:.)=-]\s*", "", norm_str(p)) for p in re.split(r"[,;]| and ", got)]
        parts = [p for p in parts if p]
        return parts == [norm_str(x) for x in expected], got
    if kind == "ints":
        nums = [int(x) for x in re.findall(r"\d+", got)]
        return nums == list(expected), got
    if kind == "colour":
        words = set(re.findall(r"[a-z]+", got.lower()))
        colours = {"red", "green", "blue", "yellow", "orange", "purple", "black", "white", "pink", "brown", "grey", "gray", "cyan", "magenta"}
        return expected in words and not ((words & colours) - {expected}), got
    raise ValueError(kind)


def extract_code(text, name):
    text = strip_think(text)
    blocks = re.findall(r"```[ \t]*(python|py|python3)?[^\n]*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    py = [b for lang, b in blocks if lang]
    plain = [b for lang, b in blocks if not lang]
    if not blocks:
        m = re.search(r"```[ \t]*(?:python|py|python3)?[^\n]*\n(.*)$", text, re.DOTALL | re.IGNORECASE)
        if m:  # unterminated fence (truncated answer)
            py = [m.group(1)]
    cands = py or plain
    if not cands:
        return text if re.search(r"^\s*(def|class|import|from)\s", text, re.MULTILINE) else None
    pat = re.compile(r"^\s*(def|class)\s+" + re.escape(name) + r"\b", re.MULTILINE)
    for b in reversed(cands):
        if pat.search(b):
            return b
    return cands[-1]


def extract_json(text):
    t = strip_think(text)
    m = re.match(r"^```[ \t]*(?:json)?[ \t]*\n(.*?)\n?```\s*$", t, re.DOTALL | re.IGNORECASE)
    if m:
        t = m.group(1)
    return json.loads(t)


def extract_html(text):
    t = strip_think(text)
    blocks = re.findall(r"```[ \t]*(?:html|htm)?[^\n]*\n(.*?)```", t, re.DOTALL | re.IGNORECASE)
    blocks = [b for b in blocks if "<" in b]
    if blocks:
        return max(blocks, key=len)
    m = re.search(r"```[ \t]*html[^\n]*\n(.*)$", t, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(<!doctype html.*?</html>|<html.*?</html>)", t, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(<!doctype html.*|<html.*)", t, re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else None


# ----------------------------------------------------------------------------
# test definitions
# ----------------------------------------------------------------------------

ANS_SUFFIX = "\n\nThink it through, then give the final answer on the last line in the form `ANSWER: <answer>`."

REASONING_TESTS = [
    {"id": "rsn-revenue", "kind": "num", "expected": "538.75",
     "prompt": "A bakery sells muffins at $3.00 each and cookies at $1.25 each. On Monday it sold 47 muffins and "
               "132 cookies. On Tuesday it sold 38 muffins and 95 cookies. What was the bakery's total revenue in "
               "dollars over the two days?"},
    {"id": "rsn-dice", "kind": "num", "expected": "5/18",
     "prompt": "Two fair six-sided dice are rolled. What is the probability that the sum is 8 or that both dice "
               "show the same number? Give the answer as a reduced fraction."},
    {"id": "rsn-logic-grid", "kind": "list", "expected": ["Ana", "Dev", "Cleo", "Ben"],
     "prompt": "Four people (Ana, Ben, Cleo, Dev) live in four houses in a row, numbered 1 to 4 from left to right, "
               "one person per house. Each owns a different pet: a cat, a dog, a parrot, a fish.\n"
               "1. The fish owner lives in house 4.\n"
               "2. Ben lives immediately to the right of the cat owner (his house number is exactly one higher).\n"
               "3. Cleo owns neither the dog nor the fish.\n"
               "4. Dev lives in an even-numbered house.\n"
               "5. Ana lives next door to the dog owner.\n"
               "6. Cleo does not live in house 1.\n"
               "7. Ana does not own the cat.\n"
               "Who lives in each house? Give the four names in house order 1, 2, 3, 4, separated by commas."},
    {"id": "rsn-date", "kind": "date", "expected": "2025-12-09",
     "prompt": "What is the calendar date exactly 1000 days after 15 March 2023? Give it in YYYY-MM-DD format."},
    {"id": "rsn-letter-count", "kind": "num", "expected": "14",
     "prompt": "How many times does the letter 'r' (upper or lower case) appear in this sentence?\n"
               "The rural juror reported three rare errors during the trial."},
    {"id": "rsn-reverse", "kind": "str", "expected": "hsalF-6202kramhcneB",
     "prompt": "Reverse the following string character by character and give the result: Benchmark2026-Flash"},
    {"id": "rsn-divisible-sum", "kind": "num", "expected": "53679",
     "prompt": "What is the sum of all integers from 1 to 500 inclusive that are divisible by 3 or by 7 (or both)?"},
    {"id": "rsn-pipes", "kind": "num", "expected": "36/7",
     "prompt": "Pipe A fills a tank in 6 hours and pipe B fills it in 9 hours. Drain C empties a full tank in 12 hours. "
               "The tank starts empty and all three are opened at the same time. How many hours does it take to fill "
               "the tank? Give the answer as a reduced fraction."},
]

THINK_PUZZLE = {
    "id": "decode-thinking", "expected_knights": ["E"],
    "prompt": "Five people, A, B, C, D and E, are each either a knight (always tells the truth) or a knave "
              "(always lies).\nA says: \"Exactly one of B and C is a knight.\"\nB says: \"E is a knave.\"\n"
              "C says: \"Exactly two of us five are knights.\"\nD says: \"A and E are the same type.\"\n"
              "E says: \"If D is a knight, then C is a knight.\"\nDetermine who is a knight and who is a knave. "
              "Check every statement. On the last line write `ANSWER: ` followed by the letters of the knights, "
              "comma separated.",
}

# ---- coding tasks: prompt, hidden tests, reference, broken reference ----

CODE_TESTS = []


def _code(id, name, spec, tests, ref, broken, forbid=False):
    CODE_TESTS.append({"id": id, "name": name, "spec": spec, "tests": tests, "ref": ref,
                       "broken": broken, "forbid": forbid})


_code("code-merge-intervals", "merge_intervals",
      "Write a Python function `merge_intervals(intervals: list[list[int]]) -> list[list[int]]`.\n"
      "Each interval is [start, end] with start <= end (closed). Merge all overlapping intervals; intervals that only "
      "touch (e.g. [1, 4] and [4, 5]) also merge. Return a new list of [start, end] lists sorted by start. The input "
      "may be unsorted and may be empty. Do not modify the input list.",
      '''
def _n(r): return [list(x) for x in r]
def test_basic(m): assert _n(m.merge_intervals([[1,3],[2,6],[8,10],[15,18]])) == [[1,6],[8,10],[15,18]]
def test_touching(m): assert _n(m.merge_intervals([[1,4],[4,5]])) == [[1,5]]
def test_unsorted(m): assert _n(m.merge_intervals([[5,7],[1,2],[2,3],[6,8]])) == [[1,3],[5,8]]
def test_empty(m): assert _n(m.merge_intervals([])) == []
def test_contained(m): assert _n(m.merge_intervals([[1,10],[2,3],[4,5]])) == [[1,10]]
def test_points(m): assert _n(m.merge_intervals([[3,3],[1,1],[1,2]])) == [[1,2],[3,3]]
def test_negative(m): assert _n(m.merge_intervals([[-5,-1],[-2,0],[3,4]])) == [[-5,0],[3,4]]
def test_gap(m): assert _n(m.merge_intervals([[1,2],[3,4]])) == [[1,2],[3,4]]
def test_no_mutation(m):
    x = [[2,3],[1,2],[7,9]]
    m.merge_intervals(x)
    assert x == [[2,3],[1,2],[7,9]]
def test_random(m):
    import random
    rng = random.Random(7)
    for _ in range(200):
        iv = []
        for _ in range(rng.randint(0, 12)):
            a = rng.randint(-20, 40); iv.append([a, a + rng.randint(0, 8)])
        exp = []
        for a, b in sorted(iv):
            if exp and a <= exp[-1][1]: exp[-1][1] = max(exp[-1][1], b)
            else: exp.append([a, b])
        assert _n(m.merge_intervals([list(x) for x in iv])) == exp, iv
''',
      '''
def merge_intervals(intervals):
    out = []
    for a, b in sorted([list(x) for x in intervals]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out
''',
      '''
def merge_intervals(intervals):
    out = []
    for a, b in sorted([list(x) for x in intervals]):
        if out and a < out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out
''')

_code("code-roman", "roman_to_int",
      "Write a Python function `roman_to_int(s: str) -> int` that converts an uppercase Roman numeral in canonical "
      "form (values 1 to 3999, standard subtractive notation: IV, IX, XL, XC, CD, CM) to an integer. It must raise "
      "ValueError for any string that is not a valid canonical Roman numeral, for example '', 'IIII', 'IC', 'VX', "
      "'MMMM' or 'ABC'.",
      '''
def _to_roman(n):
    vals = [(1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),(50,"L"),(40,"XL"),(10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")]
    out = ""
    for v, s in vals:
        while n >= v: out += s; n -= v
    return out
def test_examples(m):
    for s, v in [("III",3),("IV",4),("IX",9),("LVIII",58),("MCMXCIV",1994),("MMMCMXCIX",3999),("XL",40),("CDXLIV",444),("MMXXVI",2026),("I",1)]:
        assert m.roman_to_int(s) == v, s
def test_all(m):
    for n in range(1, 4000):
        assert m.roman_to_int(_to_roman(n)) == n, n
def test_invalid(m):
    for s in ["", "IIII", "IC", "VX", "IL", "MMMM", "VV", "DD", "IIV", "ABC", "XIIII", "LL", "IM", "XD", "CCCC"]:
        try:
            m.roman_to_int(s)
        except ValueError:
            continue
        raise AssertionError("no ValueError for %r" % s)
''',
      '''
_V = [(1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),(50,"L"),(40,"XL"),(10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")]
def _to(n):
    out = ""
    for v, s in _V:
        while n >= v:
            out += s; n -= v
    return out
_TABLE = {_to(n): n for n in range(1, 4000)}
def roman_to_int(s):
    if s not in _TABLE:
        raise ValueError(s)
    return _TABLE[s]
''',
      '''
def roman_to_int(s):
    v = {"I":1,"V":5,"X":10,"L":50,"C":100,"D":500,"M":1000}
    if not s or any(c not in v for c in s):
        raise ValueError(s)
    total = 0
    for i, c in enumerate(s):
        if i + 1 < len(s) and v[c] < v[s[i+1]]: total -= v[c]
        else: total += v[c]
    return total
''')

_code("code-top-k-words", "top_k_words",
      "Write a Python function `top_k_words(text: str, k: int) -> list[str]`.\n"
      "A word is a maximal run of ASCII letters a-z or A-Z; every other character (digits, apostrophes, hyphens, "
      "punctuation, whitespace) separates words. Words are compared case-insensitively and returned in lowercase. "
      "Return the k most frequent words, ordered by frequency descending; words with equal frequency are ordered "
      "alphabetically ascending. If there are fewer than k distinct words return all of them. If k <= 0 return [].",
      '''
def test_basic(m): assert m.top_k_words("the cat and the hat and the bat", 2) == ["the", "and"]
def test_ties(m): assert m.top_k_words("b a c b a c d", 2) == ["a", "b"]
def test_case(m): assert m.top_k_words("Apple apple APPLE banana Banana cherry", 3) == ["apple", "banana", "cherry"]
def test_separators(m): assert m.top_k_words("don't stop-it, it's 2fast4you", 3) == ["it", "don", "fast"]
def test_fewer(m): assert m.top_k_words("two one", 5) == ["one", "two"]
def test_zero(m): assert m.top_k_words("a a b", 0) == []
def test_empty(m): assert m.top_k_words("", 3) == [] and m.top_k_words("123 !!", 2) == []
def test_random(m):
    import random, re
    from collections import Counter
    rng = random.Random(11)
    vocab = ["alpha","Beta","gamma","delta","EPS","zeta","eta","theta"]
    for _ in range(100):
        text = "".join(rng.choice(vocab) + rng.choice([" ", ", ", "-", "1", "'", "\\n", "  "]) for _ in range(rng.randint(0, 60)))
        k = rng.randint(-1, 9)
        c = Counter(w.lower() for w in re.findall(r"[A-Za-z]+", text))
        exp = [w for w, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))][:max(k, 0)]
        assert m.top_k_words(text, k) == exp, (text, k)
''',
      '''
import re
from collections import Counter
def top_k_words(text, k):
    if k <= 0:
        return []
    c = Counter(w.lower() for w in re.findall(r"[A-Za-z]+", text))
    return [w for w, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))][:k]
''',
      '''
import re
from collections import Counter
def top_k_words(text, k):
    c = Counter(w.lower() for w in re.findall(r"[A-Za-z]+", text))
    return [w for w, _ in c.most_common(max(k, 0))]
''')


def _expr_cases():
    rng = random.Random(5)

    def gen(depth):
        if depth <= 0 or rng.random() < 0.3:
            n = rng.randint(0, 50)
            s = str(n) if rng.random() < 0.8 else f"{n}.{rng.randint(1, 9)}"
            return ("-" + s) if rng.random() < 0.15 else s
        a, b = gen(depth - 1), gen(depth - 1)
        op = rng.choice("+-*/")
        e = f"{a} {op} {b}"
        return f"({e})" if rng.random() < 0.5 else e
    num = re.compile(r"\d+(?:\.\d+)?")
    cases = []
    while len(cases) < 150:
        e = gen(4)
        # our own generated arithmetic only: evaluate exactly (Fraction) and in floats, keep well-conditioned cases
        try:
            exact = eval(num.sub(lambda m: f"F('{m.group()}')", e), {"__builtins__": {}, "F": Fraction})
            flt = eval(num.sub(lambda m: m.group() if "." in m.group() else m.group() + ".0", e), {"__builtins__": {}})
        except ZeroDivisionError:
            continue
        if abs(flt) > 1e9 or abs(float(exact) - flt) > 1e-12 * max(1.0, abs(flt)):
            continue
        cases.append((e, flt))
    return cases


_code("code-evaluate", "evaluate",
      "Write a Python function `evaluate(expr: str) -> float` that evaluates an arithmetic expression.\n"
      "Supported: non-negative numbers written as digits with an optional decimal part (e.g. 12, 3.5, 0.25), the "
      "binary operators + - * /, unary minus (e.g. -3, -(2+1), 2 * -3, 1 - -1), parentheses and arbitrary whitespace. "
      "Use normal precedence (* and / before + and -), all binary operators left-associative, / is true division. "
      "Return the value as a float. Raise ZeroDivisionError on division by zero and ValueError for malformed input "
      "(e.g. '', '1 +', '(1 + 2', '1 2', '2 * * 3', 'abc'). Do not use eval, exec, compile or the ast module.",
      '''
CASES = %s
def _close(a, b): return abs(a - b) <= 1e-9 * max(1.0, abs(b))
def test_basic(m):
    for e, v in [("1 + 2 * 3", 7), ("(1 + 2) * 3", 9), ("10 / 4", 2.5), ("2 - 3 - 4", -5), ("100 / 10 / 5", 2),
                 ("-3 + 5", 2), ("-(2 + 3) * 2", -10), ("2 * -3", -6), ("  3.5*2 ", 7), ("((2))", 2), ("1 - -1", 2),
                 ("2*(3+4)*(5-1)/7", 8), ("0.1 + 0.2", 0.30000000000000004), ("8 / 2 * 4", 16), ("7", 7)]:
        got = m.evaluate(e)
        assert isinstance(got, (int, float)) and _close(float(got), v), (e, got, v)
def test_zero_division(m):
    for e in ["1/0", "5 / (2 - 2)"]:
        try: m.evaluate(e)
        except ZeroDivisionError: continue
        raise AssertionError("no ZeroDivisionError for %%r" %% e)
def test_malformed(m):
    for e in ["", "1 +", "(1 + 2", "1 2", "2 * * 3", "abc", ")(", "1 + 2)", "()"]:
        try: m.evaluate(e)
        except ValueError: continue
        raise AssertionError("no ValueError for %%r" %% e)
def test_random(m):
    for e, v in CASES:
        got = m.evaluate(e)
        assert _close(float(got), v), (e, got, v)
''' % repr(_expr_cases()),
      '''
import re
def evaluate(expr):
    toks = re.findall(r"\\d+(?:\\.\\d+)?|[-+*/()]|\\S", expr)
    pos = [0]
    def peek(): return toks[pos[0]] if pos[0] < len(toks) else None
    def take():
        t = peek(); pos[0] += 1; return t
    def atom():
        t = take()
        if t is None: raise ValueError("unexpected end")
        if t == "-": return -atom()
        if t == "(":
            v = add()
            if take() != ")": raise ValueError("missing )")
            return v
        if re.fullmatch(r"\\d+(?:\\.\\d+)?", t): return float(t)
        raise ValueError("bad token %r" % t)
    def mul():
        v = atom()
        while peek() in ("*", "/"):
            op = take(); r = atom()
            v = v * r if op == "*" else v / r
        return v
    def add():
        v = mul()
        while peek() in ("+", "-"):
            op = take(); r = mul()
            v = v + r if op == "+" else v - r
        return v
    v = add()
    if pos[0] != len(toks): raise ValueError("trailing input")
    return float(v)
''',
      '''
import re
def evaluate(expr):
    toks = re.findall(r"\\d+(?:\\.\\d+)?|[-+*/()]|\\S", expr)
    pos = [0]
    def peek(): return toks[pos[0]] if pos[0] < len(toks) else None
    def take():
        t = peek(); pos[0] += 1; return t
    def atom():
        t = take()
        if t is None: raise ValueError("unexpected end")
        if t == "-": return -atom()
        if t == "(":
            v = expr_()
            if take() != ")": raise ValueError("missing )")
            return v
        if re.fullmatch(r"\\d+(?:\\.\\d+)?", t): return float(t)
        raise ValueError("bad token")
    def expr_():
        v = atom()
        while peek() in ("*", "/", "+", "-"):
            op = take(); r = atom()
            v = {"*": v * r, "+": v + r, "-": v - r}.get(op) if op != "/" else v / r
        return v
    v = expr_()
    if pos[0] != len(toks): raise ValueError("trailing input")
    return float(v)
''', forbid=True)

_code("code-lcs", "lcs",
      "Write a Python function `lcs(a: str, b: str) -> str` that returns a longest common subsequence of strings a "
      "and b (characters in order, not necessarily contiguous). If several exist, return any one of them. It must "
      "handle strings of up to 500 characters each quickly.",
      '''
def _len(a, b):
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]
def _is_sub(s, t):
    it = iter(t)
    return all(c in it for c in s)
def _check(m, a, b):
    r = m.lcs(a, b)
    assert isinstance(r, str), type(r)
    assert len(r) == _len(a, b), (a[:30], b[:30], r[:30], len(r), _len(a, b))
    assert _is_sub(r, a) and _is_sub(r, b), (a[:30], b[:30], r[:30])
def test_examples(m):
    for a, b in [("ABCBDAB", "BDCABA"), ("", "abc"), ("abc", ""), ("abc", "abc"), ("abc", "def"),
                 ("AGGTAB", "GXTXAYB"), ("aaaa", "aa"), ("xyz", "zyx")]:
        _check(m, a, b)
    assert m.lcs("AGGTAB", "GXTXAYB") == "GTAB"
def test_random(m):
    import random
    rng = random.Random(3)
    for _ in range(60):
        a = "".join(rng.choice("ACGT") for _ in range(rng.randint(0, 40)))
        b = "".join(rng.choice("ACGT") for _ in range(rng.randint(0, 40)))
        _check(m, a, b)
def test_large(m):
    import random
    rng = random.Random(4)
    a = "".join(rng.choice("ACGTU") for _ in range(500))
    b = "".join(rng.choice("ACGTU") for _ in range(500))
    _check(m, a, b)
''',
      '''
def lcs(a, b):
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] else max(dp[i + 1][j], dp[i][j + 1])
    i = j = 0
    out = []
    while i < n and j < m:
        if a[i] == b[j]:
            out.append(a[i]); i += 1; j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return "".join(out)
''',
      '''
def lcs(a, b):
    out, j = [], 0
    for c in a:
        k = b.find(c, j)
        if k >= 0:
            out.append(c); j = k + 1
    return "".join(out)
''')

_code("code-rate-limiter", "RateLimiter",
      "Write a Python class `RateLimiter` with `__init__(self, max_calls: int, period: float)` and a method "
      "`allow(self, t: float) -> bool` implementing a sliding-window rate limiter. `t` is the time of a call in "
      "seconds; successive calls have non-decreasing t. A call at time t is allowed if and only if fewer than "
      "max_calls previously *allowed* calls have a timestamp strictly greater than t - period. Allowed calls are "
      "recorded; rejected calls are not recorded and do not count. Return True if allowed, else False. Separate "
      "instances must not share state, and each call should be fast even after 100000 calls.",
      '''
def test_basic(m):
    r = m.RateLimiter(3, 10)
    seq = [(0, True), (1, True), (2, True), (3, False), (9.99, False), (10, True), (10, False), (11, True), (11.5, False), (12, True)]
    for t, exp in seq:
        assert r.allow(t) == exp, (t, exp)
def test_one(m):
    r = m.RateLimiter(1, 1.0)
    for t, exp in [(0, True), (0.5, False), (1.0, True), (1.5, False), (2.0, True), (2.0, False), (3.5, True)]:
        assert r.allow(t) == exp, (t, exp)
def test_same_time(m):
    r = m.RateLimiter(2, 5)
    assert [r.allow(5), r.allow(5), r.allow(5)] == [True, True, False]
def test_rejected_do_not_count(m):
    r = m.RateLimiter(2, 10)
    assert [r.allow(0), r.allow(1)] == [True, True]
    for t in range(2, 10):
        assert r.allow(t) == False, t
    assert r.allow(10) == True    # call at 0 expired; rejected calls ignored
    assert r.allow(10.5) == False
    assert r.allow(11) == True
def test_independent(m):
    a, b = m.RateLimiter(1, 100), m.RateLimiter(1, 100)
    assert a.allow(0) == True and b.allow(0) == True and a.allow(1) == False
def test_many(m):
    import time
    r = m.RateLimiter(50, 1000)
    t0 = time.time()
    allowed = sum(1 for i in range(100000) if r.allow(i))
    assert allowed == 5000, allowed
    assert time.time() - t0 < 3.0
''',
      '''
from collections import deque
class RateLimiter:
    def __init__(self, max_calls, period):
        self.max_calls, self.period, self.q = max_calls, period, deque()
    def allow(self, t):
        while self.q and self.q[0] <= t - self.period:
            self.q.popleft()
        if len(self.q) < self.max_calls:
            self.q.append(t)
            return True
        return False
''',
      '''
from collections import deque
class RateLimiter:
    def __init__(self, max_calls, period):
        self.max_calls, self.period, self.q = max_calls, period, deque()
    def allow(self, t):
        while self.q and self.q[0] <= t - self.period:
            self.q.popleft()
        self.q.append(t)
        return len(self.q) <= self.max_calls
''')

CODE_SUFFIX = ("\n\nGive the complete implementation (with any imports it needs) in a single ```python code block at "
               "the end of your answer. Do not include example usage or tests in that block.")

FORBID_RE = re.compile(r"(?<![\w.])(eval|exec|compile)\s*\(|^\s*(import\s+ast\b|from\s+ast\s+import)", re.MULTILINE)

RUNNER = r'''
import sys, json, signal, builtins, io, contextlib, traceback
sys.dont_write_bytecode = True
sys.path.insert(0, "/t")
cfg = json.load(open("/t/cfg.json"))
out = {"passed": 0, "total": 0, "failures": [], "import_error": None}
class _Timeout(BaseException):
    pass
def _alarm(signum, frame):
    raise _Timeout("timed out")
signal.signal(signal.SIGALRM, _alarm)
def emit():
    sys.__stdout__.write("\n@@RESULT@@" + json.dumps(out) + "\n")
    sys.__stdout__.flush()
buf = io.StringIO()
try:
    signal.alarm(20)
    with contextlib.redirect_stdout(buf):
        import sol
    signal.alarm(0)
except BaseException as e:
    signal.alarm(0)
    out["import_error"] = (type(e).__name__ + ": " + str(e))[:800]
    emit()
    sys.exit(0)
import tests
names = [k for k in vars(tests) if k.startswith("test_")]
out["total"] = len(names)
if cfg.get("forbid"):
    def _blocked(*a, **k):
        raise RuntimeError("eval/exec/compile are not allowed")
    builtins.eval = _blocked
    builtins.exec = _blocked
    builtins.compile = _blocked
import time as _time
_deadline = _time.time() + float(cfg.get("budget", 40))
for n in names:
    if _time.time() > _deadline:
        out["failures"].append({"test": n, "error": "not run: time budget exhausted"})
        continue
    try:
        signal.alarm(int(cfg.get("per_test", 5)))
        with contextlib.redirect_stdout(io.StringIO()):
            getattr(tests, n)(sol)
        signal.alarm(0)
        out["passed"] += 1
    except BaseException as e:
        signal.alarm(0)
        out["failures"].append({"test": n, "error": (type(e).__name__ + ": " + str(e))[:400]})
emit()
'''


def sandbox_run(code, task, image=SANDBOX_IMAGE, timeout=60):
    """Run model code against hidden tests inside a throwaway docker container."""
    root = os.path.join(HERE, ".sandbox")
    os.makedirs(root, exist_ok=True)
    tmp = os.path.join(root, uuid.uuid4().hex[:12])
    os.makedirs(tmp)
    name = "bench-sbx-" + os.path.basename(tmp)
    res = {"passed": 0, "total": None, "failures": [], "import_error": None, "sandbox_error": None,
           "sandbox_time": None}
    try:
        for fn, txt in (("sol.py", code), ("tests.py", task["tests"]), ("run.py", RUNNER),
                        ("cfg.json", json.dumps({"forbid": task.get("forbid", False), "per_test": 5,
                                                  "budget": max(5, timeout - 20)}))):
            with open(os.path.join(tmp, fn), "w") as f:
                f.write(txt)
        os.chmod(tmp, 0o755)
        cmd = ["docker", "run", "--rm", "--name", name, "--network", "none", "--memory", "1g", "--cpus", "1",
               "-v", f"{tmp}:/t:ro", "--entrypoint", "python3", image, "/t/run.py"]
        t0 = time.perf_counter()
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=30)
            res["sandbox_error"] = f"timeout after {timeout}s (container killed)"
            return res
        finally:
            res["sandbox_time"] = time.perf_counter() - t0
        stdout = p.stdout.decode("utf-8", "replace")
        marks = [ln for ln in stdout.splitlines() if ln.startswith("@@RESULT@@")]
        if not marks:
            res["sandbox_error"] = (f"no result (exit {p.returncode}): "
                                    + short(p.stderr.decode("utf-8", "replace")[-1500:] + stdout[-500:], 1500))
            return res
        res.update(json.loads(marks[-1][len("@@RESULT@@"):]))
        return res
    except Exception as e:  # noqa: BLE001
        res["sandbox_error"] = f"{type(e).__name__}: {e}"
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- strict JSON ----

JSON_TESTS = [
    {"id": "json-customer",
     "text": "Hey team, quick note from the front desk (sorry for the mess, typed this on my phone). The new sign-up "
             "yesterday was a woman named Dr. Priya Raman-Okafor; she said she's 41 (turning 42 in May, she was very "
             "keen to mention that). Her email, which she spelled out twice, is priya.ro@example.org, NOT the gmail "
             "one she first wrote on the form, that one bounced. She moved here from Leeds but now lives in Porto, "
             "Portugal. She did pay for the annual membership, so mark her as a member. Phone number wasn't given.",
     "schema": '{"name": string (full name, without any title such as Dr.), "age": integer, "email": string, '
               '"city": string (current city only), "is_member": boolean}',
     "expected": {"name": "Priya Raman-Okafor", "age": 41, "email": "priya.ro@example.org", "city": "Porto",
                  "is_member": True}},
    {"id": "json-invoice",
     "text": "INVOICE (scan, sorry for the coffee stain). Ref no. INV-2027-0042, which supersedes the draft "
             "INV-2027-0041. Issued 3 Feb 2027, payable within 30 days. Line items: 3 x SKU-A17 widget brackets @ "
             "12.50; 1 x SKU-B02 mounting rail @ 49.99; 10 x SKU-C33 hex bolts @ 0.40. Shipping 5.00 flat. All "
             "prices are in euros. Total due: 96.49. Questions? Call accounts on extension 204.",
     "schema": '{"invoice_number": string, "issue_date": string "YYYY-MM-DD", "currency": string (ISO 4217 code), '
               '"total": number, "items": array of {"sku": string, "qty": integer} in the order they appear '
               '(product lines only, not shipping)}',
     "expected": {"invoice_number": "INV-2027-0042", "issue_date": "2027-02-03", "currency": "EUR", "total": 96.49,
                  "items": [{"sku": "SKU-A17", "qty": 3}, {"sku": "SKU-B02", "qty": 1}, {"sku": "SKU-C33", "qty": 10}]}},
    {"id": "json-event",
     "text": "Minutes, sort of: the offsite planning session got moved again. It was supposed to be Tuesday at 9, "
             "then Kim pushed it, and it is now confirmed for Thursday 2027-04-15, starting at quarter past two in "
             "the afternoon. Headcount: 18 people said yes and 3 said maybe (don't count the maybes). We booked the "
             "Cedar room and the Birch room; the Maple room booking was cancelled. Catering will be vegetarian.",
     "schema": '{"date": string "YYYY-MM-DD", "start_time": string "HH:MM" in 24-hour time, "attendees": integer '
               '(confirmed only), "rooms": array of strings (room names only, e.g. "Oak", for rooms that are still '
               'booked), "catering": string}',
     "expected": {"date": "2027-04-15", "start_time": "14:15", "attendees": 18, "rooms": ["Cedar", "Birch"],
                  "catering": "vegetarian"}},
]


def json_prompt(t):
    return ("Extract the information from the text below into a JSON object with exactly these keys:\n"
            f"{t['schema']}\n\nText:\n\"\"\"\n{t['text']}\n\"\"\"\n\n"
            "Reply with only the JSON object and nothing else.")


def _jnorm(v, key=None):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    if isinstance(v, str):
        s = v.strip().lower()
        if key == "rooms":
            s = re.sub(r"\s+room$", "", s)
        if key == "name":
            s = re.sub(r"^(dr\.?|doctor)\s+", "", s)
        return s
    if isinstance(v, list):
        items = [_jnorm(x, key) for x in v]
        return sorted(items, key=repr) if key == "rooms" else items
    if isinstance(v, dict):
        return {k: _jnorm(x, k) for k, x in v.items()}
    return v


def grade_json(t, text):
    try:
        obj = extract_json(text)
    except Exception as e:  # noqa: BLE001
        return False, f"not JSON: {type(e).__name__}: {e}"
    if not isinstance(obj, dict):
        return False, "not a JSON object"
    exp = t["expected"]
    wrong = [k for k in exp if _jnorm(obj.get(k), k) != _jnorm(exp[k], k)]
    extra = [k for k in obj if k not in exp]
    if wrong or extra:
        return False, f"wrong: {wrong} extra: {extra}"
    return True, "ok"


# ---- tool calling ----

TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather", "description": "Get the current weather for a city.",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "City name, e.g. Paris"},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}}, "required": ["city", "unit"]}}},
    {"type": "function", "function": {
        "name": "calculator", "description": "Evaluate an arithmetic expression using + - * / and parentheses.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "e.g. (2+3)*4"}}, "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "search_flights", "description": "Search for flights.",
        "parameters": {"type": "object", "properties": {
            "origin": {"type": "string", "description": "Origin city or IATA code"},
            "destination": {"type": "string", "description": "Destination city or IATA code"},
            "date": {"type": "string", "description": "Departure date, YYYY-MM-DD"},
            "passengers": {"type": "integer"}}, "required": ["origin", "destination", "date", "passengers"]}}},
]


def _safe_arith(expr):
    expr = expr.replace("×", "*").replace("x", "*").replace("÷", "/").replace(",", "")
    if not re.fullmatch(r"[\d\s+\-*/().]+", expr):
        return None
    try:
        return eval(expr, {"__builtins__": {}})  # characters restricted to digits/operators above
    except Exception:  # noqa: BLE001
        return None


def _city(v, names):
    v = str(v or "").strip().lower()
    return any(n in v for n in names)


TOOL_TESTS = [
    {"id": "tool-weather", "prompt": "What's the weather like in Tokyo right now? Please give it in Fahrenheit.",
     "expect": "get_weather",
     "check": lambda a: _city(a.get("city"), ["tokyo"]) and str(a.get("unit", "")).lower() == "fahrenheit"},
    {"id": "tool-calculator", "prompt": "Use the calculator to work out 1234 * 5678 + 91.", "expect": "calculator",
     "check": lambda a: _safe_arith(str(a.get("expression", ""))) == 7006743},
    {"id": "tool-flights", "prompt": "Find me flights from Boston to Denver on March 3rd, 2027 for 2 passengers.",
     "expect": "search_flights",
     "check": lambda a: (_city(a.get("origin"), ["boston", "bos"]) and _city(a.get("destination"), ["denver", "den"])
                         and str(a.get("date", "")).strip() == "2027-03-03" and str(a.get("passengers")) == "2")},
    {"id": "tool-none", "prompt": "What is the capital of France? Answer in one word.", "expect": None,
     "check": None},
]


def grade_tool(t, r):
    calls = r.get("tool_calls") or []
    if t["expect"] is None:
        if calls:
            return False, f"unexpected tool call {calls[0].get('name')}"
        ok = "paris" in strip_think(r.get("content", "")).lower()
        return ok, "no tool call" + ("" if ok else ", answer missing Paris")
    if not calls:
        return False, "no tool call"
    for c in calls:
        if c.get("name") != t["expect"]:
            continue
        try:
            args = json.loads(c.get("arguments") or "{}")
        except ValueError:
            return False, f"arguments not JSON: {short(c.get('arguments'), 200)}"
        if not isinstance(args, dict):
            return False, "arguments not an object"
        if t["check"](args):
            return True, json.dumps(args)
        return False, "wrong args " + json.dumps(args)
    return False, "wrong function " + ",".join(str(c.get("name")) for c in calls)


# ---- vision ----

VISION_TESTS = [
    {"id": "vision-shapes", "img": img_shapes, "kind": "ints", "expected": [3, 2],
     "prompt": "Look at the image. How many red squares are there, and how many blue circles? On the last line write "
               "`ANSWER: <number of red squares>, <number of blue circles>`."},
    {"id": "vision-bars", "img": img_bars, "kind": "num", "expected": "3",
     "prompt": "This is a bar chart with four bars, numbered 1 to 4 from left to right. Which bar is the tallest? On "
               "the last line write `ANSWER: <bar number>`."},
    {"id": "vision-quadrants", "img": img_quadrants, "kind": "colour", "expected": "green",
     "prompt": "The image is split into four coloured quadrants. What colour is the top-right quadrant? On the last "
               "line write `ANSWER: <colour>`."},
]

# ---- instruction following ----


def grade_vowels(text):
    t = strip_think(text)
    lines = t.split("\n")
    if len(lines) != 5:
        return False, f"{len(lines)} lines"
    firsts = []
    for ln in lines:
        s = ln.strip()
        if not s:
            return False, "empty line"
        firsts.append(s[0].upper())
    if set(firsts) != set("AEIOU"):
        return False, "first letters " + "".join(firsts)
    return True, "".join(firsts)


def grade_twelve(text):
    t = strip_think(text)
    if "\n" in t:
        return False, "more than one line"
    if not t.endswith("."):
        return False, "no final period"
    words = t[:-1].split()
    bad = [w for w in words if not re.fullmatch(r"[a-z]+", w)]
    if bad:
        return False, f"bad words {bad[:5]}"
    if len(words) != 12:
        return False, f"{len(words)} words"
    if "ocean" not in words:
        return False, "no 'ocean'"
    return True, "12 words"


IF_TESTS = [
    {"id": "if-five-vowels", "grader": grade_vowels,
     "prompt": "Reply with exactly 5 lines. Each line must start with a different vowel (A, E, I, O, U; one line "
               "each, any order). Do not write any other text: no title, no numbering, no blank lines."},
    {"id": "if-twelve-words", "grader": grade_twelve,
     "prompt": "Write one sentence about the ocean that contains exactly 12 words and includes the word \"ocean\". "
               "Use only lowercase letters, no digits, and no punctuation at all except a single period at the very "
               "end. Reply with only that sentence."},
]

HTML_TESTS = [
    {"id": "html-solar-system",
     "prompt": "Create a single-file HTML page (inline CSS and JavaScript, no external resources) showing an "
               "animated solar system on a <canvas>: the sun in the centre, all eight planets on elliptical orbits "
               "at different speeds, Saturn's rings, a moon orbiting Earth, a starfield background, and controls to "
               "pause and change the speed. Reply with the complete file in one ```html code block."},
    {"id": "html-snake",
     "prompt": "Create a single-file HTML page (inline CSS and JavaScript, no external resources) with a playable "
               "Snake game on a canvas: arrow keys and WASD, food, growing snake, score and high score (localStorage), "
               "increasing speed, pause on space, game-over screen with restart. Reply with the complete file in one "
               "```html code block."},
    {"id": "html-synth-landing",
     "prompt": "Create a single-file HTML landing page (inline CSS, minimal JavaScript, no external resources or "
               "images) for a fictional analogue synthesizer called the \"Halcyon VX-9\": hero section with a "
               "synth drawn in pure CSS, feature grid, specs table, three pricing tiers, testimonials, FAQ accordion "
               "and footer. Polished, responsive, dark theme. Reply with the complete file in one ```html code block."},
]


# ----------------------------------------------------------------------------
# ability items: one list shared by the HTTP run, --dump-prompts and --grade-answers
# ----------------------------------------------------------------------------

OFFLINE_TPC = 0.30   # tokens/char assumed for the offline long-context document (no server to measure)


def longctx_prompt(target, tpc):
    """Deterministic long-context prompt for a target size. Returns (text, expected, detail)."""
    rng = random.Random(4242 + target)
    recs = gen_records(rng, int((target - 250) / tpc))
    vals = {"ALPHA": rng.randint(100, 999), "BETA": rng.randint(100, 999), "GAMMA": rng.randint(100, 999)}
    n = len(recs)
    for name, frac in (("GAMMA", 0.85), ("BETA", 0.5), ("ALPHA", 0.12)):
        recs.insert(int(n * frac), f"Note: the vault ledger states that fragment {name} of the vault code "
                                   f"is {vals[name]}.\n")
    q = ("\n\nSomewhere in the records above, three notes give the fragments ALPHA, BETA and GAMMA of the "
         "vault code. What is the sum of those three numbers?" + ANS_SUFFIX)
    text = "Here is a log of courier records.\n\n" + "".join(recs) + q
    return text, str(sum(vals.values())), {"target_tokens": target, "tokens_per_char_assumed": tpc, "values": vals}


TOOL_OFFLINE_SUFFIX = (
    "\n\n---\nYou can call the following tools (OpenAI function-calling format):\n\n{tools}\n\n"
    "Respond with exactly one JSON object and nothing else, in this form:\n"
    '{{"tool": <name of the tool to call, or null if no tool is needed>, "arguments": {{<arguments for the tool, '
    'or empty>}}, "answer": <your direct text answer if no tool is called, otherwise null>}}')


def tool_offline_prompt(prompt):
    return prompt + TOOL_OFFLINE_SUFFIX.format(tools=json.dumps(TOOLS, indent=2))


def ability_items(tpc_fn, max_ctx, offline=False):
    """All ability tests in run order. Each item: id, cat, expected, and text (or lazy() -> text,
    expected, detail), optionally image (callable -> PNG bytes) and test (the source definition)."""
    items = []
    for t in REASONING_TESTS:
        items.append({"id": t["id"], "cat": "reasoning", "test": t, "expected": t["expected"],
                      "text": t["prompt"] + ANS_SUFFIX})
    for t in CODE_TESTS:
        items.append({"id": t["id"], "cat": "coding", "test": t, "expected": "all hidden tests pass",
                      "text": t["spec"] + CODE_SUFFIX})
    for t in JSON_TESTS:
        items.append({"id": t["id"], "cat": "json", "test": t, "expected": t["expected"], "text": json_prompt(t)})
    for t in TOOL_TESTS:
        items.append({"id": t["id"], "cat": "tools", "test": t, "expected": t["expect"] or "no tool call",
                      "text": t["prompt"]})
    for t in VISION_TESTS:
        items.append({"id": t["id"], "cat": "vision", "test": t, "expected": t["expected"], "text": t["prompt"],
                      "image": t["img"]})
    for tid, target in (("longctx-30k", 30000), ("longctx-100k", 100000)):
        if offline and target > 30000:
            continue
        target = min(target, max_ctx - 6000 - 1500)
        it = {"id": tid, "cat": "longctx", "expected": None, "skip": target < 2000}
        it["lazy"] = (lambda target=target: longctx_prompt(target, tpc_fn()))
        items.append(it)
    for t in IF_TESTS:
        items.append({"id": t["id"], "cat": "instruct", "test": t, "expected": "rule satisfied", "text": t["prompt"]})
    if offline:
        items.append({"id": "decode-thinking", "cat": "thinking", "expected": THINK_PUZZLE["expected_knights"],
                      "text": THINK_PUZZLE["prompt"]})
    return items


def materialize(it):
    if "lazy" in it and "text" not in it:
        it["text"], it["expected"], it["detail"] = it["lazy"]()
    return it["text"]


def grade_item(it, r, sandbox_image=SANDBOX_IMAGE, sandbox_timeout=60):
    """Grade a successful reply r (needs r['content'] and r['tool_calls']). Returns (status, got, detail)."""
    cat, t, content = it["cat"], it.get("test"), r.get("content") or ""
    detail = it.get("detail")
    if cat in ("reasoning", "vision"):
        ok, got = grade_answer(t["kind"], t["expected"], content)
        return ("pass" if ok else "fail"), got, detail
    if cat == "longctx":
        ok, got = grade_answer("num", it["expected"], content)
        return ("pass" if ok else "fail"), got, detail
    if cat == "thinking":
        got = answer_line(content)
        knights = sorted(set(re.findall(r"\b[A-E]\b", got))) if got is not None else None
        return ("pass" if knights == it["expected"] else "fail"), got, detail
    if cat == "json":
        ok, why = grade_json(t, content)
        return ("pass" if ok else "fail"), why, detail
    if cat == "tools":
        ok, why = grade_tool(t, r)
        return ("pass" if ok else "fail"), why, detail
    if cat == "instruct":
        ok, why = t["grader"](content)
        return ("pass" if ok else "fail"), why, detail
    if cat == "coding":
        code = extract_code(content, t["name"])
        if not code:
            return "fail", "no code block", None
        if t.get("forbid") and FORBID_RE.search(code):
            return "fail", "uses eval/exec/compile/ast", {"code": code}
        sb = sandbox_run(code, t, sandbox_image, sandbox_timeout)
        sb["code"] = code
        if sb.get("sandbox_error"):
            return "error", "sandbox error", sb
        if sb.get("import_error"):
            return "fail", "import error: " + sb["import_error"], sb
        ok = sb["passed"] == sb["total"] and sb["total"]
        return ("pass" if ok else "fail"), f"{sb['passed']}/{sb['total']} tests", sb
    raise ValueError(cat)


# ----------------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------------

class Bench:
    def __init__(self, args):
        self.a = args
        self.c = Client(args.base, args.model, args.timeout, args.api_key)
        self.think_off = args.think_off
        self.think_on = args.think_on
        self.out = os.path.abspath(args.out)
        self.outdir = os.path.dirname(self.out)
        self.lock = threading.Lock()
        self.cal = {}
        self.res = {
            "meta": {"label": args.label, "model": args.model, "base": args.base,
                     "started": datetime.datetime.now().isoformat(timespec="seconds"),
                     "think_off": self.think_off, "think_on": self.think_on, "max_ctx": args.max_ctx,
                     "timeout": args.timeout, "only": args.only, "filter": args.filter,
                     "tps_definition": "(completion_tokens - 1) / (time of last delta - time of first delta)"},
            "speed": {}, "ability": {"tests": [], "summary": {}}, "html": [],
        }

    # -- utils
    def want(self, tid):
        return not self.a.filter or re.search(self.a.filter, tid)

    def body(self, messages, think, **kw):
        b = {"model": self.a.model, "messages": messages}
        b.update(kw)
        return deep_merge(b, self.think_on if think else self.think_off)

    def save(self):
        with self.lock:
            os.makedirs(self.outdir, exist_ok=True)
            tmp = self.out + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.res, f, indent=1, default=str)
            os.replace(tmp, self.out)

    def log(self, sec, name, r, extra=""):
        if "answer_file" in r:
            status = "answer file " + ("MISSING" if r.get("error") else "read")
        elif r.get("error"):
            status = "ERROR " + short(r["error"], 110)
        else:
            status = (f"ttft {fmt(r.get('ttft'))}s  {fmt(r.get('completion_tokens'))} tok "
                      f"(r {fmt(r.get('reasoning_tokens'))})  {fmt(r.get('tps'), 1)} tok/s  total {fmt(r.get('total_time'), 1)}s")
        print(f"[{sec:7}] {name:26} {status}  {extra}".rstrip(), flush=True)

    # -- calibration: prompt tokens per character for a text generator
    def calibrate(self, kind):
        if kind in self.cal:
            return self.cal[kind]
        default = 0.33 if kind == "words" else 0.30
        rng = random.Random(99)
        sample = gen_text(rng, 12000) if kind == "words" else "".join(gen_records(rng, 12000))
        base = self.c.chat(self.body([{"role": "user", "content": "Reply with OK."}], False, max_tokens=1,
                                     temperature=0), timeout=min(self.a.timeout, 300))
        full = self.c.chat(self.body([{"role": "user", "content": sample + "\nReply with OK."}], False, max_tokens=1,
                                     temperature=0), timeout=min(self.a.timeout, 300))
        if base.get("prompt_tokens") and full.get("prompt_tokens") and full["prompt_tokens"] > base["prompt_tokens"]:
            tpc = (full["prompt_tokens"] - base["prompt_tokens"]) / len(sample)
            src = "measured"
        else:
            tpc, src = default, "default (calibration failed: %s)" % (full.get("error") or base.get("error") or "no usage")
        self.cal[kind] = tpc
        self.res["meta"].setdefault("calibration", {})[kind] = {"tokens_per_char": tpc, "source": src}
        print(f"[calib  ] {kind:26} {tpc:.4f} tokens/char ({src})", flush=True)
        return tpc

    # ------------------------------------------------------------------ SPEED
    def run_speed(self):
        S = self.res["speed"]
        prose = "Write an 800-word essay on the history of the transistor."
        code = ("Write a long, complete Python module containing: (1) an LRUCache class with O(1) get and put using a "
                "dict and a hand-written doubly linked list, with capacity eviction and __len__/__contains__; (2) a "
                "Trie class with insert, search, starts_with, delete and a method listing all words with a prefix; "
                "(3) a tokenizer for arithmetic expressions producing (type, value, position) tuples with clear "
                "error messages; and (4) a comprehensive unittest test suite covering all three. Include docstrings "
                "and type hints. Output only the code.")
        for tid, prompt, mt in (("decode-prose", prose, 1000), ("decode-code", code, 1500)):
            if not self.want(tid):
                continue
            runs = []
            for i in range(3):
                r = self.c.chat(self.body([{"role": "user", "content": prompt}], False, max_tokens=mt, temperature=0.7))
                self.log("speed", f"{tid} run{i + 1}", r)
                runs.append(public(r))
            ok = [x for x in runs if not x["error"]]
            S[tid] = {"runs": runs, "n_ok": len(ok),
                      "median_tps": median([x["tps"] for x in ok]), "median_ttft": median([x["ttft"] for x in ok]),
                      "median_total": median([x["total_time"] for x in ok]),
                      "median_tokens": median([x["completion_tokens"] for x in ok])}
            self.save()

        if self.want("decode-thinking"):
            r = self.c.chat(self.body([{"role": "user", "content": THINK_PUZZLE["prompt"]}], True, max_tokens=3000,
                                      temperature=0.6))
            got = answer_line(r.get("content", ""))
            knights = sorted(set(re.findall(r"\b[A-E]\b", got))) if got is not None else None
            correct = knights == THINK_PUZZLE["expected_knights"]
            self.log("speed", "decode-thinking", r, f"answer={got!r} correct={correct}")
            S["decode-thinking"] = dict(public(r), answer=got, correct=correct,
                                        expected="knights: " + ",".join(THINK_PUZZLE["expected_knights"]))
            self.save()

        prefill_16k = None
        if self.want("prefill"):
            tpc = self.calibrate("words")
            sizes = [4000, 16000, 64000]
            big = int(0.9 * self.a.max_ctx)
            if big > 64000:
                sizes.append(big)
            sizes = [s for s in sizes if s <= 0.9 * self.a.max_ctx + 1]
            rows = []
            seed = random.SystemRandom().randrange(1 << 62)
            for i, target in enumerate(sizes):
                rng = random.Random(seed + i * 7919)
                code_word = f"{rng.choice(ADJ)}-{rng.randint(1000, 9999)}"
                chars = int((target - 60) / tpc)
                head = f"[document {seed:x}-{i}]\n" + gen_text(rng, chars // 2)
                tail = gen_text(rng, chars - chars // 2)
                doc = head + f"\n\nImportant: the secret code word is {code_word}. Remember it.\n\n" + tail
                q = "\n\nWhat is the secret code word mentioned in the text above? Reply with only the code word."
                r = self.c.chat(self.body([{"role": "user", "content": doc + q}], False, max_tokens=30, temperature=0))
                found = code_word.lower() in strip_think(r.get("content", "")).lower()
                self.log("speed", f"prefill-{target // 1000}k", r,
                         f"prompt {fmt(r.get('prompt_tokens'))} tok  {fmt(r.get('prompt_tps'), 0)} pp tok/s  found={found}")
                row = dict(public(r), target_tokens=target, code_word=code_word, found=found, prompt_chars=len(doc + q))
                rows.append(row)
                if target == 16000:
                    prefill_16k = (doc, code_word, row)
                S["prefill"] = rows
                self.save()

        if self.want("cached-prefill"):
            if prefill_16k is None:
                tpc = self.calibrate("words")
                rng = random.Random(random.SystemRandom().randrange(1 << 62))
                code_word = f"{rng.choice(ADJ)}-{rng.randint(1000, 9999)}"
                chars = int(15940 / tpc)
                doc = (gen_text(rng, chars // 2) + f"\n\nImportant: the secret code word is {code_word}. Remember it.\n\n"
                       + gen_text(rng, chars - chars // 2))
                warm = self.c.chat(self.body([{"role": "user", "content": doc + "\n\nWhat is the secret code word? "
                                              "Reply with only the code word."}], False, max_tokens=30, temperature=0))
                self.log("speed", "cached-prefill warmup", warm)
                first = public(warm)
            else:
                doc, code_word, first = prefill_16k
            q2 = "\n\nRepeat the secret code word from the text above in uppercase, with nothing else."
            r = self.c.chat(self.body([{"role": "user", "content": doc + q2}], False, max_tokens=30, temperature=0))
            found = code_word.lower() in strip_think(r.get("content", "")).lower()
            self.log("speed", "cached-prefill-16k", r,
                     f"ttft {fmt(r.get('ttft'))}s vs uncached {fmt(first.get('ttft'))}s found={found}")
            S["cached-prefill"] = dict(public(r), found=found, uncached_ttft=first.get("ttft"),
                                       speedup=(first["ttft"] / r["ttft"]) if first.get("ttft") and r.get("ttft") else None)
            self.save()

        if self.want("parallel"):
            S["parallel"] = {}
            for n in (2, 4):
                results = [None] * n
                barrier = threading.Barrier(n)

                def worker(k):
                    try:
                        barrier.wait(timeout=30)
                    except threading.BrokenBarrierError:
                        pass
                    results[k] = self.c.chat(self.body([{"role": "user", "content": prose}], False, max_tokens=1000,
                                                       temperature=0.7))
                t0 = time.perf_counter()
                ths = [threading.Thread(target=worker, args=(k,), daemon=True) for k in range(n)]
                for t in ths:
                    t.start()
                for t in ths:
                    t.join()
                wall = time.perf_counter() - t0
                ok = [x for x in results if x and not x["error"]]
                firsts = [x["_tfirst_abs"] for x in ok if x.get("_tfirst_abs")]
                lasts = [x["_tlast_abs"] for x in ok if x.get("_tlast_abs")]
                tot = sum(x["completion_tokens"] or 0 for x in ok)
                agg = tot / (max(lasts) - min(firsts)) if firsts and lasts and max(lasts) > min(firsts) else None
                row = {"streams": n, "n_ok": len(ok), "wall_time": wall, "total_tokens": tot,
                       "aggregate_tps": agg, "aggregate_tps_wall": tot / wall if wall > 0 else None,
                       "per_stream_tps_mean": statistics.mean([x["tps"] for x in ok if x["tps"]]) if any(x["tps"] for x in ok) else None,
                       "per_stream_tps_min": min([x["tps"] for x in ok if x["tps"]], default=None),
                       "runs": [public(x) for x in results if x],
                       "errors": [x["error"] for x in results if x and x["error"]]}
                err = f"  errors {len(row['errors'])}: {short(row['errors'][0], 80)}" if row["errors"] else ""
                print(f"[speed  ] parallel-{n:<17} aggregate {fmt(agg, 1)} tok/s  per-stream {fmt(row['per_stream_tps_mean'], 1)} "
                      f"tok/s  wall {fmt(wall, 1)}s{err}", flush=True)
                S["parallel"][str(n)] = row
                self.save()

    # ---------------------------------------------------------------- ABILITY
    def ability_req(self, messages, **kw):
        return self.c.chat(self.body(messages, True, max_tokens=6000, temperature=0.2, **kw))

    def record(self, cat, tid, r, status, expected, got, detail=None):
        row = {"id": tid, "category": cat, "status": status, "pass": status == "pass", "expected": expected,
               "got": got, "detail": detail}
        row.update({k: v for k, v in public(r).items()})
        self.res["ability"]["tests"].append(row)
        s = self.res["ability"]["summary"].setdefault(cat, {"pass": 0, "fail": 0, "error": 0, "unsupported": 0,
                                                            "no_answer": 0, "total": 0})
        s[status] += 1
        s["total"] += 1
        self.log("ability", tid, r, f"=> {status.upper()}  got={short(got, 60)!s}")
        self.save()

    def run_ability(self):
        for it in ability_items(lambda: self.calibrate("records"), self.a.max_ctx):
            if not self.want(it["id"]):
                continue
            if it.get("skip"):
                print(f"[ability] {it['id']:26} skipped (max-ctx too small)", flush=True)
                continue
            text = materialize(it)
            content = text
            if it.get("image"):
                content = [{"type": "text", "text": text},
                           {"type": "image_url", "image_url": {"url": data_url(it["image"]())}}]
            kw = {"tools": TOOLS, "tool_choice": "auto"} if it["cat"] == "tools" else {}
            r = self.ability_req([{"role": "user", "content": content}], **kw)
            if r["error"]:
                status = "error"
                if it["cat"] == "vision":
                    st = r.get("http_status")
                    low = r["error"].lower()
                    if (st is not None and 400 <= st < 500) or (
                            st is not None and st >= 500 and re.search(r"image|vision|multimodal|modalit|not support", low)):
                        status = "unsupported"
                detail = dict(it["detail"], error=r["error"]) if it.get("detail") else r["error"]
                self.record(it["cat"], it["id"], r, status, it["expected"], None, detail)
                continue
            status, got, detail = grade_item(it, r, self.a.sandbox_image, self.a.sandbox_timeout)
            self.record(it["cat"], it["id"], r, status, it["expected"], got, detail)

    # ------------------------------------------------------------------- HTML
    def run_html(self):
        hdir = os.path.join(self.outdir, "html")
        for t in HTML_TESTS:
            if not self.want(t["id"]):
                continue
            r = self.c.chat(self.body([{"role": "user", "content": t["prompt"]}], True, max_tokens=12000,
                                      temperature=0.7))
            html = extract_html(r.get("content", ""))
            path = None
            if html:
                os.makedirs(hdir, exist_ok=True)
                path = os.path.join(hdir, f"{self.a.label}-{t['id'][5:]}.html")
                with open(path, "w") as f:
                    f.write(html)
            self.log("html", t["id"], r, f"saved={os.path.relpath(path) if path else 'NO HTML'}")
            self.res["html"].append(dict(public(r), id=t["id"], file=path, html_chars=len(html) if html else 0))
            self.save()

    # ---------------------------------------------------------------- report
    def summary_lines(self):
        S, A = self.res.get("speed") or {}, self.res["ability"]["summary"]
        L = [f"# Benchmark: {self.a.label} ({self.a.model})", "",
             f"Started {self.res['meta']['started']}, finished {self.res['meta'].get('finished', '-')}, " +
             (f"Offline answers graded from `{self.res['meta'].get('answers_dir')}`." if self.res["meta"].get("mode") == "offline" else
              f"base `{self.a.base}`, think-off `{json.dumps(self.think_off)}`, think-on `{json.dumps(self.think_on)}`."),
             ""]
        if S:
            L += ["## Speed", "", "| test | ttft s | tokens | tok/s | total s | notes |", "|---|---|---|---|---|---|"]
        for tid in ("decode-prose", "decode-code"):
            if tid in S:
                d = S[tid]
                L.append(f"| {tid} (median of {d['n_ok']}/3) | {fmt(d['median_ttft'])} | {fmt(d['median_tokens'])} | "
                         f"{fmt(d['median_tps'], 1)} | {fmt(d['median_total'], 1)} | |")
        if "decode-thinking" in S:
            d = S["decode-thinking"]
            L.append(f"| decode-thinking | {fmt(d['ttft'])} | {fmt(d['completion_tokens'])} (reasoning "
                     f"{fmt(d['reasoning_tokens'])}, answer {fmt(d['answer_tokens'])}) | {fmt(d['tps'], 1)} | "
                     f"{fmt(d['total_time'], 1)} | correct={fmt(d['correct'])} {short(d.get('error') or '', 60)} |")
        for row in S.get("prefill", []):
            L.append(f"| prefill ~{row['target_tokens'] // 1000}k | {fmt(row['ttft'])} | prompt {fmt(row['prompt_tokens'])} | "
                     f"{fmt(row['prompt_tps'], 0)} (prompt) | {fmt(row['total_time'], 1)} | found={fmt(row['found'])} "
                     f"{short(row.get('error') or '', 60)} |")
        if "cached-prefill" in S:
            d = S["cached-prefill"]
            L.append(f"| cached-prefill 16k | {fmt(d['ttft'])} | prompt {fmt(d['prompt_tokens'])} | uncached ttft "
                     f"{fmt(d['uncached_ttft'])} s, speedup {fmt(d['speedup'], 1)}x | {fmt(d['total_time'], 1)} | "
                     f"found={fmt(d['found'])} {short(d.get('error') or '', 60)} |")
        for n, d in (S.get("parallel") or {}).items():
            L.append(f"| parallel x{n} | - | {d['total_tokens']} | aggregate {fmt(d['aggregate_tps'], 1)}, per-stream "
                     f"{fmt(d['per_stream_tps_mean'], 1)} | {fmt(d['wall_time'], 1)} | ok {d['n_ok']}/{n} |")
        L += ["", "## Ability", "", "| category | pass | fail | error | unsupported | no answer |", "|---|---|---|---|---|---|"]
        for cat, s in A.items():
            L.append(f"| {cat} | {s['pass']}/{s['total']} | {s['fail']} | {s['error']} | {s['unsupported']} | "
                     f"{s.get('no_answer', 0)} |")
        tp = sum(s["pass"] for s in A.values())
        tt = sum(s["total"] for s in A.values())
        L.append(f"| **all** | **{tp}/{tt}** | | | | |")
        L += ["", "| test | status | expected | got | tokens (reasoning) | time s |", "|---|---|---|---|---|---|"]
        for t in self.res["ability"]["tests"]:
            got = t["got"] if t["status"] != "error" else t.get("error")
            L.append(f"| {t['id']} | {t['status']} | {short(json.dumps(t['expected']) if not isinstance(t['expected'], str) else t['expected'], 50)} | "
                     f"{short(got, 70).replace('|', '/')} | {fmt(t.get('completion_tokens'))} ({fmt(t.get('reasoning_tokens'))}) | "
                     f"{fmt(t.get('total_time'), 1)} |")
        if self.res.get("html"):
            L += ["", "## HTML", "", "| page | file | tokens (reasoning) | tok/s | time s | finish |", "|---|---|---|---|---|---|"]
            for h in self.res["html"]:
                f = os.path.relpath(h["file"], self.outdir) if h["file"] else "none"
                L.append(f"| {h['id']} | {f} | {fmt(h['completion_tokens'])} ({fmt(h['reasoning_tokens'])}) | "
                         f"{fmt(h['tps'], 1)} | {fmt(h['total_time'], 1)} | {h.get('finish_reason') or short(h.get('error'), 50)} |")
        return L

    def run(self):
        only = set((self.a.only or "speed,ability,html").split(","))
        self.res["meta"]["server_models"] = sm = self.c.get_models()
        if isinstance(sm, dict) and sm.get("error"):
            print(f"WARNING: GET {self.a.base}/models failed ({sm['error']}); continuing, errors will be recorded",
                  flush=True)
        else:
            ids = [m.get("id") for m in (sm.get("data") or []) if isinstance(m, dict)] if isinstance(sm, dict) else []
            print(f"server models: {ids}", flush=True)
            if ids and self.a.model not in ids:
                print(f"WARNING: --model {self.a.model!r} is not in the server's model list", flush=True)
        self.save()
        t0 = time.time()
        for sec, fn in (("speed", self.run_speed), ("ability", self.run_ability), ("html", self.run_html)):
            if sec in only:
                try:
                    fn()
                except Exception as e:  # noqa: BLE001  (should not happen; keep whatever we have)
                    traceback.print_exc()
                    self.res.setdefault("crashes", []).append(f"{sec}: {type(e).__name__}: {e}")
        self.res["meta"]["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
        self.res["meta"]["wall_seconds"] = time.time() - t0
        self.save()
        lines = self.summary_lines()
        md = os.path.join(self.outdir, f"{self.a.label}.md")
        with open(md, "w") as f:
            f.write("\n".join(lines) + "\n")
        print("\n" + "\n".join(l for l in lines if not l.startswith("|---")))
        print(f"\nwrote {self.out} and {md}")


# ----------------------------------------------------------------------------
# self test (no server needed)
# ----------------------------------------------------------------------------

def selftest(args):
    fails = 0

    def check(cond, msg):
        nonlocal fails
        print(("  ok   " if cond else "  FAIL ") + msg, flush=True)
        if not cond:
            fails += 1

    print("graders:")
    canon = {"num": lambda e: e, "str": lambda e: e, "date": lambda e: e, "list": lambda e: ", ".join(e)}
    for t in REASONING_TESTS:
        e = canon[t["kind"]](t["expected"])
        check(grade_answer(t["kind"], t["expected"], f"blah\n**ANSWER:** {e}.")[0], f"{t['id']} accepts {e!r}")
        check(not grade_answer(t["kind"], t["expected"], "ANSWER: 12345 wrong")[0], f"{t['id']} rejects wrong")
    check(grade_answer("num", "538.75", "ANSWER: $538.75")[0], "dollar sign")
    check(grade_answer("num", "5/18", "ANSWER: 0.2778")[0], "decimal for fraction")
    check(grade_answer("num", "53679", "ANSWER: 53,679")[0], "thousands comma")
    check(grade_answer("str", "hsalF-6202kramhcneB", "ANSWER: `HSALF-6202KRAMHCNEB`")[0], "case-insensitive string")
    check(not grade_answer("num", "14", "no answer line")[0], "missing ANSWER line fails")
    check(grade_answer("num", "14", "<think>ANSWER: 3</think>\nANSWER: 14")[0], "think tags stripped")
    check(grade_answer("ints", [3, 2], "ANSWER: 3 red squares, 2 blue circles")[0], "vision ints")
    check(grade_answer("colour", "green", "ANSWER: Green.")[0], "colour")
    check(not grade_answer("colour", "green", "ANSWER: green or blue")[0], "colour ambiguity rejected")
    for t in JSON_TESTS:
        check(grade_json(t, "```json\n" + json.dumps(t["expected"]) + "\n```")[0], f"{t['id']} accepts expected")
        bad = dict(t["expected"]); k = list(bad)[0]; bad[k] = "zzz"
        check(not grade_json(t, json.dumps(bad))[0], f"{t['id']} rejects wrong")
    check(not grade_json(JSON_TESTS[0], "Here: " + json.dumps(JSON_TESTS[0]["expected"]))[0], "json with prose rejected")
    ex = JSON_TESTS[2]["expected"]
    check(grade_json(JSON_TESTS[2], json.dumps(dict(ex, rooms=["birch room", "Cedar"])))[0], "rooms lenient")
    good_args = {"tool-weather": {"city": "tokyo", "unit": "fahrenheit"}, "tool-calculator": {"expression": "1234*5678+91"},
                 "tool-flights": {"origin": "BOS", "destination": "Denver, CO", "date": "2027-03-03", "passengers": 2}}
    for t in TOOL_TESTS:
        if t["expect"]:
            r = {"tool_calls": [{"name": t["expect"], "arguments": json.dumps(good_args[t["id"]])}]}
            check(grade_tool(t, r)[0], f"{t['id']} accepts good call")
            check(not grade_tool(t, {"tool_calls": [], "content": "x"})[0], f"{t['id']} rejects no call")
        else:
            check(grade_tool(t, {"tool_calls": [], "content": "Paris."})[0], "tool-none accepts Paris")
            check(not grade_tool(t, {"tool_calls": [{"name": "get_weather", "arguments": "{}"}]})[0], "tool-none rejects call")
    check(grade_vowels("Apple\nEgg\nIgloo\nOwl\nUmbrella")[0], "vowels ok")
    check(not grade_vowels("1. Apple\nEgg\nIgloo\nOwl\nUmbrella")[0], "vowels numbering rejected")
    check(grade_twelve("the ocean is wide and deep and blue and full of life.")[0], "twelve ok")
    check(not grade_twelve("The ocean is wide and deep and blue and full of life.")[0], "twelve uppercase rejected")
    check(extract_code("x\n```python\ndef f(): pass\n```\n```python\nprint(f())\n```", "f").strip() == "def f(): pass",
          "extract_code prefers block defining the name")
    check(extract_html("Here\n```html\n<!DOCTYPE html><html></html>\n```") == "<!DOCTYPE html><html></html>\n", "extract_html")

    print("images:")
    for t in VISION_TESTS:
        png = t["img"]()
        check(png.startswith(b"\x89PNG") and len(png) > 500, f"{t['id']} png {len(png)} bytes")
        if args.dump_images:
            os.makedirs(args.dump_images, exist_ok=True)
            with open(os.path.join(args.dump_images, t["id"] + ".png"), "wb") as f:
                f.write(png)

    print("sandbox (docker):")
    for t in CODE_TESTS:
        good = sandbox_run(t["ref"], t, args.sandbox_image, args.sandbox_timeout)
        check(not good["sandbox_error"] and good["passed"] == good["total"] and good["total"],
              f"{t['id']} reference passes {good['passed']}/{good['total']} {short(good['sandbox_error'] or good['failures'], 200)} ({fmt(good['sandbox_time'], 1)}s)")
        bad = sandbox_run(t["broken"], t, args.sandbox_image, args.sandbox_timeout)
        check(not bad["sandbox_error"] and bad["passed"] < (bad["total"] or 0),
              f"{t['id']} broken fails {bad['passed']}/{bad['total']} {short(bad['sandbox_error'] or [f['test'] for f in bad['failures']], 200)}")
    t = CODE_TESTS[0]
    r = sandbox_run("def merge_intervals(x):\n    while True: pass\n", t, args.sandbox_image, args.sandbox_timeout)
    check(r["passed"] == 0 and not r["sandbox_error"] and all("timed out" in f["error"] or "budget" in f["error"] for f in r["failures"]),
          "infinite loop is cut by per-test alarm")
    r = sandbox_run("import sys\nsys.exit(3)\n", t, args.sandbox_image, args.sandbox_timeout)
    check(bool(r["import_error"]), f"sys.exit at import recorded: {r['import_error']}")
    r = sandbox_run("import urllib.request\nurllib.request.urlopen('http://1.1.1.1', timeout=3)\n", t,
                    args.sandbox_image, args.sandbox_timeout)
    check(bool(r["import_error"]), f"no network in sandbox: {short(r['import_error'], 80)}")
    r = sandbox_run("open('/t/x','w')\n", t, args.sandbox_image, args.sandbox_timeout)
    check(bool(r["import_error"]) and "Read-only" in r["import_error"], f"mount is read-only: {short(r['import_error'], 80)}")
    ev = CODE_TESTS[3]
    r = sandbox_run("def evaluate(e):\n    return float(__builtins__['eval'](e) if isinstance(__builtins__, dict) else __import__('builtins').eval(e))\n",
                    ev, args.sandbox_image, args.sandbox_timeout)
    check(r["passed"] < r["total"], f"runtime eval block works ({r['passed']}/{r['total']})")
    check(bool(FORBID_RE.search("x = eval(s)")) and not FORBID_RE.search("p = re.compile('x')\ndef evaluate(e): pass"),
          "static eval check")
    if args.selftest_slow:
        r = sandbox_run("import signal, time\nsignal.signal(signal.SIGALRM, signal.SIG_IGN)\nwhile True: time.sleep(1)\n",
                        t, args.sandbox_image, 15)
        check(bool(r["sandbox_error"]) and "timeout" in r["sandbox_error"], f"docker timeout kills container: {r['sandbox_error']}")
        left = subprocess.run(["docker", "ps", "-q", "--filter", "name=bench-sbx-"], capture_output=True, text=True).stdout.strip()
        check(left == "", "no sandbox containers left running")
    leftover = os.listdir(os.path.join(HERE, ".sandbox")) if os.path.isdir(os.path.join(HERE, ".sandbox")) else []
    check(leftover == [], "sandbox temp dirs cleaned")
    print(f"\nselftest: {fails} failure(s)")
    return 1 if fails else 0


# ----------------------------------------------------------------------------
# offline mode: dump prompts for a model without an HTTP endpoint, grade its answer files
# ----------------------------------------------------------------------------

def offline_items():
    return ability_items(lambda: OFFLINE_TPC, 10 ** 9, offline=True)


def dump_prompts(folder):
    os.makedirs(folder, exist_ok=True)
    idx = ["# Offline ability tests", "",
           "No system prompt is used; each `<test-id>.prompt.txt` is the complete user message.",
           "For each test, write the model's complete reply to `<test-id>.answer.txt` in this folder.",
           "Vision tests: the image `<test-id>.png` is attached to the prompt (image after the text).",
           "Tool tests: the prompt file also lists the tools and asks for one JSON object "
           "`{\"tool\": name-or-null, \"arguments\": {...}, \"answer\": text-or-null}` as the whole reply.", "",
           "| # | test id | category | prompt | image | answer file to write |", "|---|---|---|---|---|---|"]
    for n, it in enumerate(offline_items(), 1):
        text = materialize(it)
        if it["cat"] == "tools":
            text = tool_offline_prompt(text)
        pf = f"{it['id']}.prompt.txt"
        with open(os.path.join(folder, pf), "w") as f:
            f.write(text)
        img = ""
        if it.get("image"):
            img = f"{it['id']}.png"
            with open(os.path.join(folder, img), "wb") as f:
                f.write(it["image"]())
        idx.append(f"| {n} | {it['id']} | {it['cat']} | {pf} | {img or '-'} | {it['id']}.answer.txt |")
        extra = f" (~{len(text) * OFFLINE_TPC / 1000:.0f}k tokens est.)" if it["cat"] == "longctx" else ""
        print(f"wrote {pf}{' + ' + img if img else ''}{extra}")
    with open(os.path.join(folder, "INDEX.md"), "w") as f:
        f.write("\n".join(idx) + "\n")
    print(f"wrote {os.path.join(folder, 'INDEX.md')}")
    return 0


def parse_tool_reply(text):
    """Offline tool answer -> (r-like dict, error or None)."""
    t = strip_think(text)
    m = re.search(r"```[ \t]*(?:json)?[ \t]*\n(.*?)```", t, re.DOTALL | re.IGNORECASE)
    if m:
        t = m.group(1)
    try:
        obj = json.loads(t.strip())
    except ValueError as e:
        return None, f"reply is not JSON: {e}"
    if not isinstance(obj, dict):
        return None, "reply is not a JSON object"
    name, args = obj.get("tool"), obj.get("arguments")
    calls = []
    if name not in (None, "", "null", "none"):
        if not isinstance(args, dict):
            args = {}
        calls = [{"id": None, "name": str(name), "arguments": json.dumps(args)}]
    ans = obj.get("answer")
    return {"content": "" if ans is None else str(ans), "tool_calls": calls}, None


def grade_answers(a):
    folder = a.grade_answers
    b = Bench(a)
    b.res["meta"].update({"mode": "offline", "answers_dir": os.path.abspath(folder), "model": a.model})
    for k in ("base", "think_off", "think_on", "max_ctx", "timeout", "only"):
        b.res["meta"].pop(k, None)
    b.res.pop("speed", None)
    b.res.pop("html", None)
    for it in offline_items():
        if not b.want(it["id"]):
            continue
        materialize(it)   # sets expected/detail for lazily built tests
        path = os.path.join(folder, f"{it['id']}.answer.txt")
        r = {"error": None, "content": "", "reasoning": "", "tool_calls": [], "completion_tokens": None,
             "reasoning_tokens": None, "ttft": None, "tps": None, "total_time": None, "answer_file": path}
        if not os.path.exists(path):
            r["error"] = "no answer file"
            b.record(it["cat"], it["id"], r, "no_answer", it["expected"], None, it.get("detail"))
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        r["content"] = text
        if it["cat"] == "tools":
            parsed, err = parse_tool_reply(text)
            if err:
                b.record(it["cat"], it["id"], r, "fail", it["expected"], err, it.get("detail"))
                continue
            r.update(parsed)
            r["raw_reply"] = text
        try:
            status, got, detail = grade_item(it, r, a.sandbox_image, a.sandbox_timeout)
        except Exception as e:  # noqa: BLE001
            status, got, detail = "error", f"grader crashed: {type(e).__name__}: {e}", None
        b.record(it["cat"], it["id"], r, status, it["expected"], got, detail)
    b.res["meta"]["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    b.save()
    lines = b.summary_lines()
    md = os.path.join(b.outdir, f"{a.label}.md")
    with open(md, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n" + "\n".join(l for l in lines if not l.startswith("|---")))
    print(f"\nwrote {b.out} and {md}")
    return 0


# ----------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default="http://localhost:8888/v1")
    p.add_argument("--model", default=None, help="model name sent in requests")
    p.add_argument("--label", default=None, help="short name used for output files")
    p.add_argument("--out", default=None, help="result JSON path (default results/<label>.json)")
    p.add_argument("--think-off", default=json.dumps(DEFAULT_THINK_OFF), help="JSON merged into bodies, reasoning off")
    p.add_argument("--think-on", default=json.dumps(DEFAULT_THINK_ON), help="JSON merged into bodies, reasoning on")
    p.add_argument("--only", default=None, help="speed, ability, html (comma separated for several)")
    p.add_argument("--filter", default=None, help="regex on test ids, e.g. 'prefill|code-'")
    p.add_argument("--max-ctx", type=int, default=120000)
    p.add_argument("--timeout", type=float, default=900.0, help="per-request timeout, seconds")
    p.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    p.add_argument("--sandbox-image", default=SANDBOX_IMAGE)
    p.add_argument("--sandbox-timeout", type=float, default=60.0)
    p.add_argument("--selftest", action="store_true", help="check graders, images and docker sandbox; no server")
    p.add_argument("--selftest-slow", action="store_true", help="with --selftest: also test the docker timeout kill")
    p.add_argument("--dump-images", default=None, help="with --selftest: write the vision PNGs to this folder")
    p.add_argument("--dump-prompts", metavar="DIR", default=None,
                   help="write the ability prompts (and PNGs, INDEX.md) for an offline test-taker; no server")
    p.add_argument("--grade-answers", metavar="DIR", default=None,
                   help="grade <test-id>.answer.txt files in DIR (needs --label); no server")
    a = p.parse_args(argv)
    if a.selftest:
        return selftest(a)
    if a.dump_prompts:
        return dump_prompts(a.dump_prompts)
    if a.grade_answers:
        if not a.label:
            p.error("--grade-answers needs --label")
        a.model = a.model or a.label
        a.out = a.out or os.path.join("results", a.label + ".json")
        a.think_off, a.think_on = {}, {}
        return grade_answers(a)
    if not a.model or not a.label:
        p.error("--model and --label are required")
    if a.only:
        bad = set(a.only.split(",")) - {"speed", "ability", "html"}
        if bad:
            p.error(f"--only: unknown section(s) {sorted(bad)}")
    for k in ("think_off", "think_on"):
        try:
            v = json.loads(getattr(a, k))
            assert isinstance(v, dict)
        except Exception:  # noqa: BLE001
            p.error(f"--{k.replace('_', '-')} must be a JSON object")
        setattr(a, k, v)
    a.out = a.out or os.path.join("results", a.label + ".json")
    Bench(a).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
