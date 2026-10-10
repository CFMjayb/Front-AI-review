"""Beacon DEV test emails stay open in Front (Jay, 2026-10-10).

Beacon's dev site redirects every email to Jay and starts the subject "[DEV — would have gone to: <address>]". The Chief of Staff
was archiving them out of his Front inbox (the AI called a sign-in code "spam"; a sender rule archived the other). Claims proven here:
  1. Those subjects are recognised, and ordinary subjects (even ones that mention DEV) are not.
  2. The pipeline leaves such a conversation OPEN: no archive, no status change, no AI review; only the processed tag.
  3. A dry run touches nothing.
  4. A normal conversation still goes down the normal path (the same function reaches the message fetch), so the early exit is narrow.
"""
import pipeline
from modules.prefilter import is_beacon_dev_test_email

EM = "—"          # the em dash Beacon's Test Mode writes


class _Front:
    def __init__(self):
        self.calls = []
        self.fetched = []

    def add_tag(self, cid, tag):
        self.calls.append(("add_tag", cid, tag))

    def set_status(self, cid, status):
        self.calls.append(("set_status", cid, status))

    def get_conversation_messages(self, cid):
        self.fetched.append(cid)
        raise RuntimeError("reached the normal path")


class _Claude:
    def __getattr__(self, name):                       # any use of the AI is a failure
        raise AssertionError("the AI must not be used for a Beacon DEV test email")


def _conv(subject, cid="cnv_t1"):
    return {"id": cid, "subject": subject, "status": "open"}


def test_beacon_dev_subjects_are_recognised():
    assert is_beacon_dev_test_email(_conv(f"[DEV {EM} would have gone to: jboggs326@gmail.com] Your sign-in code: 123456"))
    assert is_beacon_dev_test_email(_conv(f"[DEV {EM} would have gone to: jay@cfmins.org] Your Beacon sign-in code: 654321"))
    assert is_beacon_dev_test_email(_conv("[DEV - would have gone to: x@y.org] Approval needed"))      # a plain hyphen too
    assert is_beacon_dev_test_email(_conv("  [dev – would have gone to: x@y.org] spaces and case"))


def test_ordinary_subjects_are_not_matched():
    for s in ("Your sign-in code: 123456", "Your Beacon sign-in code: 654321", "[DEV] stand-up notes", "Development budget",
              "Re: lunch [DEV team]", f"Fwd: [DEV {EM} would have gone to: x@y.org] forwarded", "", None):
        assert not is_beacon_dev_test_email(_conv(s)), s


def test_pipeline_leaves_a_dev_test_email_open():
    front = _Front()
    out = pipeline._process_one(_conv(f"[DEV {EM} would have gone to: jay@cfmins.org] Your Beacon sign-in code: 1"), front, _Claude(), dry_run=False)
    assert out["prefiltered"] and not out["errored"] and out["modules"] == {"dev_test_email": "left_open"}
    assert front.calls == [("add_tag", "cnv_t1", pipeline.PROCESSED_TAG)]         # processed tag only: never archived or resolved
    assert front.fetched == []                                                      # not even fetched


def test_a_dry_run_changes_nothing():
    front = _Front()
    pipeline._process_one(_conv(f"[DEV {EM} would have gone to: jay@cfmins.org] x"), front, _Claude(), dry_run=True)
    assert front.calls == [] and front.fetched == []


def test_a_normal_conversation_still_takes_the_normal_path():
    front = _Front()
    pipeline._process_one(_conv("Your Beacon sign-in code: 1", cid="cnv_n1"), front, _Claude(), dry_run=False)
    assert front.fetched == ["cnv_n1"]                                              # the normal path reached the message fetch
