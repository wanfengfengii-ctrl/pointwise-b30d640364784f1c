import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import check_project
import demo
from archive_db import ConflictError, NotFound, ReplyLost
from corrections.pending import FieldsBusy
from corrections.recover import CancelledCorrection, resume
from corrections.request import amend
from delivery.current import current
from delivery.reconcile import reconcile
from delivery.snapshot import frozen
from intake.register import ingest, initialise_archive
from layout.registry import define


BOTH = (demo.A_CHANGES[0], *demo.B_CHANGES)
BASE = (1, 3, 2)
ORIGINAL = (None, None, None)


def expect_error(error, operation, *args, **kwargs):
    try:
        operation(*args, **kwargs)
    except error as caught:
        return caught
    raise AssertionError(f"{operation.__name__} did not raise {error.__name__}")


def pause(event, operation, *args, **kwargs):
    loss = expect_error(ReplyLost, operation, *args, hook=demo.lose_at(event), **kwargs)
    assert loss.event == event, loss
    return loss.detail


def sample(path, volume):
    initialise_archive(path)
    define(path, demo.LAYOUT)
    receipt = ingest(path, volume, demo.LAYOUT.layout_id, demo.ROWS, demo.MANIFEST)
    return frozen(path, receipt.report_id)


def observe(path, volume, values, sources, words):
    readings = current(path, volume)
    physical = tuple(word.actual_word for word in reconcile(path, volume).words[:2])
    actual = (demo.reading_group(readings), demo.source_group(readings), physical)
    expected = (values, sources, words)
    assert actual == expected, (volume, actual, expected)
    return dict(values=actual[0], sources=actual[1], words=actual[2])


def show(case, **details):
    print(json.dumps(dict(case=case, **details), ensure_ascii=False, sort_keys=True))


def v1(path):
    volume = "v1"
    initial = sample(path, volume)
    pause("after_word_commit", amend, path, volume, "v1-a", demo.A_CHANGES)
    before = observe(path, volume, BASE, ORIGINAL, (20, 3))
    busy = expect_error(FieldsBusy, amend, path, volume, "v1-b", BOTH)
    assert (busy.holder, busy.field_name) == ("v1-a", "x")
    expect_error(NotFound, resume, path, "v1-b")
    assert resume(path, "v1-a", action="cancel") == CancelledCorrection("v1-a", volume)
    observe(path, volume, BASE, ORIGINAL, (17, 3))
    receipt = amend(path, volume, "v1-b", BOTH)
    after = observe(path, volume, (4, 3, 6), ("v1-b", None, "v1-b"), (52, 3))
    assert frozen(path, initial.report_id) == initial
    assert frozen(path, receipt.report_id).receipt == receipt
    show("exclusive-then-retry", blocked_field=busy.field_name, before=before, after=after)


def signed_report(path, receipt):
    report = frozen(path, receipt.report_id)
    assert report.receipt == receipt
    observations = tuple((item.row_id, item.field_name, item.value, item.provenance.request_id,
                          item.provenance.publication) for item in report.observations)
    assert observations == tuple((row, field, value, receipt.request_id, receipt.publication)
                                 for row, field, value in BOTH), observations
    return report


def cancel(path, volume, request):
    result = resume(path, request, action="cancel")
    assert result == CancelledCorrection(request, volume)
    assert resume(path, request) == result
    expect_error(NotFound, frozen, path, f"correction:{request}")


def handoff(path, shared, publish_first):
    volume = "publish-first" if publish_first else "cancel-first"
    alpha, beta = f"{volume}-a", f"{volume}-b"
    initial = sample(path, volume)
    pause("after_word_commit", shared, path, volume, alpha, demo.A_CHANGES)
    observe(path, volume, BASE, ORIGINAL, (20, 3))
    if publish_first:
        receipt = pause("after_publish", shared, path, volume, beta, BOTH)
        interim = observe(path, volume, (4, 3, 6), (beta, None, beta), (52, 3))
        report = signed_report(path, receipt)
        cancel(path, volume, alpha)
    else:
        pause("before_publish", shared, path, volume, beta, BOTH)
        cancel(path, volume, alpha)
        interim = observe(path, volume, BASE, ORIGINAL, (52, 3))
        initialise_archive(path)
        receipt = resume(path, beta)
        report = signed_report(path, receipt)
    initialise_archive(path)
    final = observe(path, volume, (4, 3, 6), (beta, None, beta), (52, 3))
    assert resume(path, beta) == shared(path, volume, beta, tuple(reversed(BOTH))) == receipt
    assert frozen(path, receipt.report_id) == report
    assert frozen(path, initial.report_id) == initial
    assert reconcile(path, volume).settled
    show(volume, interim=interim, final=final, report_id=receipt.report_id)


def exits(path, shared):
    for confirmed in (False, True):
        volume = "both-cancel" if confirmed else "only-admitted"
        alpha, beta = f"{volume}-a", f"{volume}-b"
        sample(path, volume)
        pause("before_publish", shared, path, volume, alpha, demo.A_CHANGES)
        event = "before_publish" if confirmed else "before_word_commit"
        pause(event, shared, path, volume, beta, BOTH)
        cancel(path, volume, alpha)
        surviving_words = (52, 3) if confirmed else (17, 3)
        interim = observe(path, volume, BASE, ORIGINAL, surviving_words)
        initialise_archive(path)
        cancel(path, volume, beta)
        final = observe(path, volume, BASE, ORIGINAL, (17, 3))
        assert reconcile(path, volume).settled
        show(volume, interim=interim, final=final)


def conflicts(path, shared):
    volume = "conflicts"
    sample(path, volume)
    pause("after_word_commit", shared, path, volume, "live", demo.A_CHANGES)
    expect_error(FieldsBusy, amend, path, volume, "retry", BOTH)
    expect_error(FieldsBusy, shared, path, volume, "retry", (("R-20", "x", 5), *demo.B_CHANGES))
    expect_error(NotFound, resume, path, "retry")
    observe(path, volume, BASE, ORIGINAL, (20, 3))
    amend(path, volume, "disjoint", demo.B_CHANGES)
    observe(path, volume, (1, 3, 6), (None, None, "disjoint"), (52, 3))
    receipt = shared(path, volume, "retry", BOTH)
    error = expect_error(ConflictError, amend, path, volume, "retry", BOTH)
    assert not isinstance(error, FieldsBusy), error
    cancel(path, volume, "live")
    final = observe(path, volume, (4, 3, 6), ("retry", None, "retry"), (52, 3))
    assert resume(path, "retry") == receipt
    show(volume, final=final)


def main():
    parser = argparse.ArgumentParser(description="Instrument archive handoff examples")
    parser.add_argument("mode", choices=("v1", "verify", "demo"))
    mode = parser.parse_args().mode
    if mode == "verify" and check_project.main():
        return 1
    if mode == "demo":
        demo.main()
    with TemporaryDirectory() as directory:
        path = Path(directory) / "shared.sqlite3"
        v1(path)
        if mode != "v1":
            from corrections.request import amend_shared

            handoff(path, amend_shared, True)
            handoff(path, amend_shared, False)
            if mode == "verify":
                exits(path, amend_shared)
                conflicts(path, amend_shared)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
