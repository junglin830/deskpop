#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deskpop — a sticky desktop notification center for AI agents and background scripts.

Toast notifications vanish after a few seconds, so you miss "Claude Code finished",
"build failed" or "needs your approval" while you are away. deskpop keeps every message
on screen (bottom-right, always on top) until you click "Got it", lets you select and copy
the text (handy for pasting errors back to an agent), and can open a URL or file with it.

    deskpop push "Build finished" --level ok
    deskpop push "Tests failed: 3" --level alert --open https://ci.example.com/run/42
    deskpop run            # start the notification window (use pythonw on Windows)

Any program can push: CLI, `import deskpop; deskpop.push(...)`, or by hooks
(Claude Code Stop/Notification hooks, Codex notify, cron, CI...).

Queue is a JSON file guarded by a cross-process lock and written atomically.
Pure standard library (tkinter), Python 3.8+. MIT License.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import traceback
import uuid
import webbrowser

__version__ = "1.0.0"

HOME = os.environ.get("DESKPOP_HOME") or os.path.join(os.path.expanduser("~"), ".deskpop")
QUEUE = os.path.join(HOME, "queue.json")
ERR_LOG = os.path.join(HOME, "error.log")
KEEP_ACKED = 200
LEVELS = ("alert", "warn", "info", "ok")


# ----------------------------------------------------------------- queue
class _Lock:
    """Cross-process lock: kernel mutex on Windows, flock elsewhere. Released by the OS on crash."""

    def __init__(self, path, timeout=10.0):
        self.path, self.timeout, self.h, self.fd = os.path.abspath(path), timeout, None, None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.CreateMutexW.restype = ctypes.c_void_p
            k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            name = "Local\\deskpop_" + hashlib.sha1(os.path.normcase(self.path).encode()).hexdigest()[:16]
            self.h = k.CreateMutexW(None, False, name)
            if k.WaitForSingleObject(self.h, int(self.timeout * 1000)) not in (0, 0x80):
                raise TimeoutError("deskpop queue lock timeout")
        else:
            import fcntl
            self.fd = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        if self.h:
            import ctypes
            k = ctypes.windll.kernel32
            k.ReleaseMutex(ctypes.c_void_p(self.h))
            k.CloseHandle(ctypes.c_void_p(self.h))
        if self.fd is not None:
            os.close(self.fd)  # closing releases the flock
        return False


def _load():
    try:
        with open(QUEUE, encoding="utf-8") as f:
            items = json.load(f)
    except FileNotFoundError:
        return []
    if not isinstance(items, list):
        raise ValueError("queue file is not a list: %s (left untouched)" % QUEUE)
    return [i for i in items if isinstance(i, dict) and i.get("id")]


def _save(items):
    acked = sorted((i for i in items if i.get("acked")), key=lambda i: i.get("acked_ts") or "")
    keep = {i["id"] for i in acked[-KEEP_ACKED:]}
    items = [i for i in items if not i.get("acked") or i["id"] in keep]  # never drop pending items
    os.makedirs(HOME, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=HOME, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, QUEUE)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def push(text, level="info", open_doc="", source="", dedup_key=""):
    """Queue a notification and return its id. Same pending dedup_key -> returns the existing id."""
    level = level if level in LEVELS else "info"
    with _Lock(QUEUE):
        items = _load()
        if dedup_key:
            for i in items:
                if i.get("dedup_key") == dedup_key and not i.get("acked"):
                    return i["id"]
        nid = "n_" + uuid.uuid4().hex[:12]
        items.append({"id": nid, "ts": _now(), "text": text or "", "level": level, "open_doc": open_doc or "",
                      "source": source or "", "dedup_key": dedup_key or "", "acked": False, "opened": False})
        _save(items)
        return nid


def _mark(nid, field):
    with _Lock(QUEUE):
        items = _load()
        for i in items:
            if i["id"] == nid:
                if not i.get(field):
                    i[field], i[field + "_ts"] = True, _now()
                    _save(items)
                return True
    return False


def ack(nid):
    return _mark(nid, "acked")


def mark_opened(nid):
    return _mark(nid, "opened")


def pending():
    return [i for i in _load() if not i.get("acked")]


def ack_all():
    with _Lock(QUEUE):
        items = _load()
        for i in items:
            if not i.get("acked"):
                i["acked"], i["acked_ts"] = True, _now()
        _save(items)


# ----------------------------------------------------------------- window
TEXT = {
    "en": {"title": "Notifications", "count": "%d", "ok": "Got it", "copy": "Copy", "open": "Open"},
    "zh-TW": {"title": "通知中心", "count": "%d 則", "ok": "知道了", "copy": "複製", "open": "開啟"},
    "zh-CN": {"title": "通知中心", "count": "%d 条", "ok": "知道了", "copy": "复制", "open": "打开"},
    "ja": {"title": "通知", "count": "%d 件", "ok": "OK", "copy": "コピー", "open": "開く"},
}
THEME = {"bg": "#1c1c1e", "card": "#2c2c2e", "border": "#38383a", "fg": "#f5f5f7", "dim": "#8e8e93",
         "accent": "#0a84ff",
         "level": {"alert": "#ff453a", "warn": "#ff9f0a", "info": "#0a84ff", "ok": "#30d158"}}
ICON = {"alert": "⛔", "warn": "⚠", "info": "●", "ok": "✔"}


def _log_err(where, exc=None):
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(ERR_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_now(), where))
            if exc is not None:
                f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) + "\n")
    except Exception:
        pass


class _Null:
    def write(self, *a, **k):
        pass

    def flush(self, *a, **k):
        pass


def _font(lang="en"):
    if sys.platform == "win32":
        return {"zh-TW": "Microsoft JhengHei UI", "zh-CN": "Microsoft YaHei UI",
                "ja": "Yu Gothic UI"}.get(lang, "Segoe UI")
    if sys.platform == "darwin":
        return "Helvetica Neue"
    return "DejaVu Sans"


def _open_target(target):
    if not target:
        return
    try:
        if target.startswith(("http://", "https://")):
            webbrowser.open(target)
        elif os.path.exists(target):
            if os.name == "nt":
                os.startfile(target)
            else:
                webbrowser.open("file://" + os.path.abspath(target))
    except Exception as e:
        _log_err("open " + target, e)


def _text_cells(s):
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)  # CJK / full width = 2 cells


def run(lang="en", max_visible=4, auto_ack_minutes=0, poll_ms=1500, auto_open=False):
    """Run the window loop. auto_ack_minutes=0 keeps notifications until acknowledged."""
    # pythonw has no console: stdout/stderr are None and any print (even Tk's own error
    # handler) would raise a second exception that kills the loop silently.
    if sys.stdout is None:
        sys.stdout = _Null()
    if sys.stderr is None:
        sys.stderr = _Null()
    import tkinter as tk

    L = TEXT.get(lang, TEXT["en"])
    F = _font(lang)
    T = THEME
    root = tk.Tk()
    root.report_callback_exception = lambda e, v, tb: _log_err("tk callback", v)
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=T["bg"])
    try:
        root.attributes("-alpha", 0.97)
    except Exception:
        pass

    def round_corners():
        if os.name != "nt":
            return
        try:
            import ctypes
            root.update_idletasks()
            pref = ctypes.c_int(2)  # DWMWCP_ROUND, Windows 11
            ctypes.windll.dwmapi.DwmSetWindowAttribute(root.winfo_id(), 33, ctypes.byref(pref), 4)
        except Exception:
            pass

    outer = tk.Frame(root, bg=T["bg"], highlightthickness=1, highlightbackground=T["border"])
    outer.pack(fill="both", expand=True)
    head = tk.Frame(outer, bg=T["bg"])
    head.pack(fill="x", padx=14, pady=(12, 6))
    tk.Label(head, text=L["title"], bg=T["bg"], fg=T["fg"], font=(F, 11, "bold")).pack(side="left")
    count = tk.Label(head, text="", bg=T["bg"], fg=T["dim"], font=(F, 9))
    count.pack(side="right")
    body = tk.Frame(outer, bg=T["bg"])
    body.pack(fill="both", expand=True, padx=10, pady=(0, 12))
    st = {"shown": False, "sig": None, "seen": {}}

    def place():
        root.update_idletasks()
        w, h = max(360, root.winfo_reqwidth()), root.winfo_reqheight()
        root.geometry("%dx%d+%d+%d" % (w, h, root.winfo_screenwidth() - w - 22, root.winfo_screenheight() - h - 56))

    def copy(text):
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
        except Exception as e:
            _log_err("copy", e)

    def do_ack(nid):
        try:
            ack(nid)
        except Exception as e:
            _log_err("ack", e)
        st["sig"] = None
        tick(reschedule=False)

    def draw(items, total):
        for c in body.winfo_children():
            c.destroy()
        count.configure(text=L["count"] % total)
        for it in items:
            col = T["level"].get(it.get("level"), T["accent"])
            card = tk.Frame(body, bg=T["card"], highlightbackground=col, highlightthickness=1)
            card.pack(fill="x", pady=4)
            top = tk.Frame(card, bg=T["card"])
            top.pack(fill="x", padx=10, pady=(9, 0))
            tk.Label(top, text=ICON.get(it.get("level"), ""), bg=T["card"], fg=col, font=(F, 11)).pack(side="left")
            src = it.get("source") or ""
            if src:
                tk.Label(top, text=src, bg=T["card"], fg=T["dim"], font=(F, 8)).pack(side="left", padx=(6, 0))
            tk.Label(top, text=(it.get("ts") or "")[-8:], bg=T["card"], fg=T["dim"], font=(F, 8)).pack(side="right")
            txt = it.get("text") or ""
            lines = sum(max(1, -(-_text_cells(ln) // 30)) for ln in (txt.split("\n") or [""]))
            tw = tk.Text(card, bg=T["card"], fg=T["fg"], font=(F, 10), width=36, height=max(1, min(lines, 12)),
                         wrap="word", relief="flat", bd=0, highlightthickness=0, cursor="xterm",
                         selectbackground=T["accent"], selectforeground="#fff", padx=0, pady=0)
            tw.insert("1.0", txt)
            tw.configure(state="disabled")  # read-only but selectable
            tw.pack(fill="x", padx=10, pady=(3, 6))
            tw.bind("<Button-1>", lambda e, w=tw: w.focus_force())
            tw.bind("<Control-c>", lambda e, w=tw, t=txt: (copy(w.get("sel.first", "sel.last") if w.tag_ranges("sel") else t), "break")[1])
            row = tk.Frame(card, bg=T["card"])
            row.pack(fill="x", padx=10, pady=(0, 9))
            btn = dict(bg=T["card"], fg=T["accent"], relief="flat", bd=0, font=(F, 9), cursor="hand2",
                       activebackground=T["card"], activeforeground=T["accent"])
            if it.get("open_doc"):
                tk.Button(row, text=L["open"], command=lambda d=it["open_doc"]: _open_target(d), **btn).pack(side="left")
            tk.Button(row, text=L["copy"], command=lambda t=txt: copy(t), **btn).pack(side="left", padx=(8, 0))
            tk.Button(row, text=L["ok"], command=lambda n=it["id"]: do_ack(n), bg=col, fg="#0b0e14", relief="flat",
                      bd=0, padx=14, pady=5, font=(F, 9, "bold"), cursor="hand2").pack(side="right")

    def tick(reschedule=True):
        try:
            items = pending()
            now = time.time()
            visible = items[-max_visible:]
            for it in visible:
                st["seen"].setdefault(it["id"], now)
                if auto_open and it.get("open_doc") and not it.get("opened"):
                    _open_target(it["open_doc"])
                    mark_opened(it["id"])
            if auto_ack_minutes:
                for it in visible:  # only messages actually shown can time out
                    if now - st["seen"][it["id"]] >= auto_ack_minutes * 60:
                        ack(it["id"])
                items = pending()
                visible = items[-max_visible:]
            live = {it["id"] for it in visible}
            st["seen"] = {k: v for k, v in st["seen"].items() if k in live}
            if not visible:
                if st["shown"]:
                    root.withdraw()
                    st["shown"] = False
                st["sig"] = None
            else:
                sig = (len(items), tuple((i["id"], i.get("text"), i.get("level")) for i in visible))
                if sig != st["sig"]:  # redraw only on change: no flicker, clicks are not interrupted
                    st["sig"] = sig
                    draw(visible, len(items))
                    if not st["shown"]:
                        root.deiconify()
                        st["shown"] = True
                        round_corners()
                    place()
        except Exception as e:
            _log_err("tick", e)
        finally:
            if reschedule:  # the loop must survive any error, or the window silently dies
                root.after(poll_ms, tick)

    tick()
    root.mainloop()


# ----------------------------------------------------------------- agent hooks
def from_claude_hook(raw):
    """Claude Code hook payload (JSON on stdin) -> notification id.

    Notification events carry a message (e.g. permission needed); Stop means the turn finished.
    """
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        d = {}
    event = d.get("hook_event_name") or ""
    where = os.path.basename((d.get("cwd") or "").rstrip("/\\")) or "session"
    if event == "Notification":
        return push(d.get("message") or "Claude Code needs your attention", "warn", source="claude-code",
                    dedup_key="cc-notify-" + str(d.get("session_id", "")))
    return push("Claude Code finished (%s)" % where, "ok", source="claude-code")


def from_codex_notify(raw):
    """Codex `notify` program payload (JSON passed as the last argv) -> notification id."""
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        d = {}
    msg = (d.get("last-assistant-message") or "").strip()
    text = "Codex finished" + (": " + msg[:300] if msg else "")
    return push(text, "ok", source="codex")


# ----------------------------------------------------------------- cli
def main(argv=None):
    ap = argparse.ArgumentParser(prog="deskpop", description="Sticky desktop notifications for agents and scripts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push", help="queue a notification")
    p.add_argument("text", help="message text ('-' reads stdin)")
    p.add_argument("--level", choices=LEVELS, default="info")
    p.add_argument("--open", dest="open_doc", default="", help="URL or file to open from the card")
    p.add_argument("--source", default="", help="short label, e.g. claude-code")
    p.add_argument("--dedup", default="", help="skip if a pending message has the same key")
    r = sub.add_parser("run", help="show the notification window")
    r.add_argument("--lang", default=os.environ.get("DESKPOP_LANG", "en"), choices=sorted(TEXT))
    r.add_argument("--max-visible", type=int, default=4)
    r.add_argument("--auto-ack", type=float, default=0, help="minutes before a shown message auto-dismisses (0=never)")
    r.add_argument("--auto-open", action="store_true", help="open attached URL/file as soon as it is shown")
    sub.add_parser("claude-hook", help="read a Claude Code hook payload from stdin")
    cx = sub.add_parser("codex-notify", help="handle a Codex notify payload (JSON argument)")
    cx.add_argument("payload", nargs="?", default="{}")
    sub.add_parser("list", help="print pending notifications")
    sub.add_parser("clear", help="acknowledge everything")
    a = ap.parse_args(argv)
    if a.cmd == "push":
        text = sys.stdin.read().strip() if a.text == "-" else a.text
        print(push(text, a.level, a.open_doc, a.source, a.dedup))
    elif a.cmd == "run":
        try:
            run(a.lang, a.max_visible, a.auto_ack, auto_open=a.auto_open)
        except Exception as e:
            _log_err("run", e)
            raise
    elif a.cmd == "claude-hook":
        from_claude_hook(sys.stdin.read())
    elif a.cmd == "codex-notify":
        from_codex_notify(a.payload)
    elif a.cmd == "list":
        for i in pending():
            print("%s %-5s %s  %s" % (i["ts"], i["level"], i["id"], i["text"].replace("\n", " ")[:80]))
    elif a.cmd == "clear":
        ack_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
