import os, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["DESKPOP_HOME"] = tempfile.mkdtemp()
os.environ["DESKPOP_LANG"] = "en"
sys.path.insert(0, os.path.join(HERE, ".."))
import deskpop as d  # noqa: E402


def test_push_ack_dedup_levels():
    a = d.push("hello", "warn", source="t")
    assert d.push("dup", dedup_key="k") == d.push("dup again", dedup_key="k")
    assert d.push("x", level="weird") and d.pending()[-1]["level"] == "info"
    assert len(d.pending()) == 3
    d.ack(a)
    assert a not in [i["id"] for i in d.pending()]
    d.ack_all()
    assert d.pending() == []


def test_parallel_cli_push_loses_nothing(n=30):
    cli = os.path.join(HERE, "..", "deskpop.py")
    before = len(d._load())
    run = lambda i: subprocess.run([sys.executable, cli, "push", "msg %d" % i], check=True,
                                   capture_output=True, env=dict(os.environ)).stdout.strip()
    with ThreadPoolExecutor(n) as ex:
        ids = list(ex.map(run, range(n)))
    assert len(set(ids)) == n
    assert len(d._load()) == before + n, "a concurrent write was lost"


def test_acked_history_bounded_pending_kept():
    d.KEEP_ACKED = 5
    for i in range(12):
        d.ack(d.push("old %d" % i))
    keep = d.push("still pending")
    items = d._load()
    assert sum(1 for i in items if i.get("acked")) <= 5
    assert any(i["id"] == keep for i in items)


def test_agent_hooks():
    d.ack_all()
    d.from_claude_hook('{"hook_event_name":"Notification","message":"Claude needs permission to use Bash","session_id":"s1","cwd":"/w/app"}')
    d.from_claude_hook('{"hook_event_name":"Notification","message":"again","session_id":"s1"}')
    d.from_claude_hook('{"hook_event_name":"Stop","cwd":"/w/app"}')
    d.from_codex_notify('{"type":"agent-turn-complete","last-assistant-message":"Rename done."}')
    d.from_claude_hook("not json")
    texts = [i["text"] for i in d.pending()]
    assert texts == ["Claude needs permission to use Bash", "Claude Code finished (app)",
                     "Codex finished: Rename done.", "Claude Code finished (session)"], texts


def test_lang_setting_and_bilingual_hooks():
    os.environ.pop("DESKPOP_LANG")
    try:
        assert d.set_lang("zh-TW+en") == d.get_lang() == "zh-TW+en"
        d.ack_all()
        d.from_claude_hook('{"hook_event_name":"Notification","message":"Claude is waiting for your input","session_id":"s9"}')
        d.from_claude_hook('{"hook_event_name":"Notification","message":"Claude needs your permission to use Bash","session_id":"s8"}')
        d.from_claude_hook('{"hook_event_name":"Stop","cwd":"/w/app"}')
        texts = [i["text"] for i in d.pending()]
        assert texts == ["Claude 在等你回覆 · Claude is waiting for your input",
                         "Claude 需要你核准使用 Bash · Claude needs your permission to use Bash",
                         "Claude Code 完成了 · finished (app)"], texts
        d.set_lang("en")
        assert d.get_lang() == "en" and d._cc_text("Claude is waiting for your input") == "Claude is waiting for your input"
        try:
            d.set_lang("xx"); assert False
        except ValueError:
            pass
        assert d.detect_lang() in d.TEXT
    finally:
        os.environ["DESKPOP_LANG"] = "en"


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v(); print("OK", k)
