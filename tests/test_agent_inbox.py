"""Who OWNS the load, and is told about it (agent_inbox, 0.4.21).

The 2026-10-16 incident: cpu-pkg-warm fired while one Claude session's subagents held
~7 cores, and the only session that could act on it was never told. These tests pin the
pure attribution: owners grouped by the messaging socket their processes inherit (Claude
session) or by Kiroshi worker manifest (worker + descendants, which wins over inherited
session env), ranked by share, a cumulative walk to 50% over an absolute floor, and a
STOP at an unaddressable top owner -- never fall through to accuse the next one.
"""
import json

from atfield import agent_inbox as ai

TOKEN = "tok-SECRET-4419"


def env(sock, sid="sess-a-0000"):
    return {"CLAUDE_CODE_MESSAGING_SOCKET": sock, "CLAUDE_CODE_MESSAGING_TOKEN": TOKEN,
            "CLAUDE_CODE_SESSION_ID": sid}


SA = env(r"\\.\pipe\LOCAL\cc-msg-a", "aaaaaaaa-1111")
SB = env(r"\\.\pipe\LOCAL\cc-msg-b", "bbbbbbbb-2222")
NONE = {}
W = {"role": "worker", "worker_id": "DEMETER-4c030c", "slots": 22,
     "launch_command": "kiroshi join --task t:run --slots 22 --job handgap-v2 --read-root x"}


def told(found):
    return [(o.kind, o.id) for o in found if o.notified]


def ranked(found):
    return [(o.kind, o.id, o.notified) for o in found]


def own(samples, workers=None, tree=None, floor=100.0):
    tree = tree or {}
    return ai.owners(samples, workers=workers or {}, descendants_of=lambda p: tree.get(p, ()),
                     floor=floor)


# -- the incident ------------------------------------------------------------------------

def incident():
    s = {100 + i: (87.5, SA) for i in range(8)}                 # A: 8 procs, 700%
    s.update({200: (0.0, SB), 201: (0.0, SB)})                  # B: idle session
    s.update({300: (0.0, NONE), 301: (0.0, NONE)})              # C: no socket
    return s


def test_the_incident_tells_exactly_the_owning_session():
    found = own(incident())
    assert told(found) == [("claude_session", "aaaaaaaa")]
    top = found[0]
    assert (top.share, top.pids, top.inbox.socket) == (1.0, 8, SA["CLAUDE_CODE_MESSAGING_SOCKET"])


def test_under_the_floor_nobody_is_told():
    # 40% of one core in total: a share of 100% over a tiny denominator means nothing
    assert told(own({100: (40.0, SA), 200: (0.0, SB)})) == []


def test_the_walk_continues_until_half_the_load_is_covered():
    s = {100: (450.0, SA), 200: (450.0, SB), 300: (100.0, NONE)}
    assert told(own(s)) == [("claude_session", "aaaaaaaa"), ("claude_session", "bbbbbbbb")]


def test_an_unaddressable_top_owner_stops_the_walk():
    s = {300: (700.0, NONE), 100: (300.0, SA)}
    assert ranked(own(s)) == [("unattributed", "-", False), ("claude_session", "aaaaaaaa", False)]


# -- the BOTH case: a Kiroshi worker whose slots inherited a session's env ------------------

def both(w_each, sa):
    s = {500: (0.0, SA), 501: (w_each, SA), 502: (w_each, SA), 503: (w_each, SA),
         100: (sa, SA), 200: (0.0, SB)}
    return own(s, workers={500: W}, tree={500: (501, 502, 503)})


def test_a_session_ahead_of_a_worker_is_the_one_told():
    found = both(100.0, 600.0)                                   # W 300%, SA 600%
    assert ranked(found) == [("claude_session", "aaaaaaaa", True),
                             ("kiroshi_worker", "DEMETER-4c030c", False),
                             ("claude_session", "bbbbbbbb", False)]


def test_a_worker_ahead_of_a_session_wins_despite_its_inherited_env():
    found = both(200.0, 300.0)                                   # W 600%, SA 300%
    assert ranked(found) == [("kiroshi_worker", "DEMETER-4c030c", True),
                             ("claude_session", "aaaaaaaa", False),
                             ("claude_session", "bbbbbbbb", False)]
    w = found[0]
    assert (w.job, w.slots, w.pids, w.inbox) == ("handgap-v2", 22, 4, None)
    assert w.public() == {"kind": "kiroshi_worker", "id": "DEMETER-4c030c", "job": "handgap-v2",
                          "slots": 22, "share": round(600 / 900, 3), "pids": 4, "notified": True}


# -- transport -----------------------------------------------------------------------------

def test_the_frame_is_the_auth_line_then_one_user_message():
    inbox = ai.inbox_of(SA)
    want = (json.dumps({"type": "auth", "token": TOKEN}) + "\n"
            + json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
    assert ai.frame(inbox, "hi") == want.encode("utf-8")


def test_the_token_never_appears_in_a_repr_or_public_view():
    found = own(incident())
    assert TOKEN not in repr(found[0].inbox)
    assert TOKEN not in repr(found)
    assert TOKEN not in json.dumps([o.public() for o in found])


def test_post_writes_the_frame_and_swallows_a_missing_pipe():
    inbox = ai.inbox_of(SA)
    written = {}

    class Pipe:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, b):
            written["b"] = b

    def opener(path, mode):
        written["path"] = path
        return Pipe()

    assert ai.post(inbox, "hi", opener=opener) is True
    assert written == {"path": SA["CLAUDE_CODE_MESSAGING_SOCKET"], "b": ai.frame(inbox, "hi")}

    def gone(path, mode):
        raise FileNotFoundError(path)
    assert ai.post(inbox, "hi", opener=gone) is False


def test_inbox_needs_both_socket_and_token():
    assert ai.inbox_of({"CLAUDE_CODE_MESSAGING_SOCKET": "x"}) is None
    assert ai.inbox_of(None) is None


def test_one_message_per_session_per_ten_minutes():
    lim = ai.RateLimit(600)
    assert [lim.allow("a", 0), lim.allow("a", 599), lim.allow("b", 1), lim.allow("a", 601)] == \
        [True, False, True, True]
