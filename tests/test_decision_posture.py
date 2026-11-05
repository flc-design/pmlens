"""The project's decision posture: .pm/decision_policy.yaml (Decision Lineage S2, PMSERV-255).

What this file pins, by docs/issues/DESIGN_decision-lineage-s2.md ("S2 spec"):

* The lenient reader and its pure view (policy.py, §2.10): one test per row of
  the reading table — no file, unreadable files (a FIFO, a link and a file over
  64 KiB return within 2 s on another thread), an unknown schema, a missing,
  unknown or non-string posture, a malformed history and its fields, the
  history scan capped at 1,000 entries from the end, and the rule that flags a
  value which disagrees with the history (an empty history included).
* The bounded reader it shares with lineage.py (``read_bounded_yaml`` and the
  size limit of ``_read_bounded``).
* The writer ``storage.change_decision_posture`` (§3.8, §5.3): the processing
  order (refusals, unchanged, not_changed for only_if_unset decided under the
  lock), a round trip back to unset, the reason redacted before saving (D11),
  the size cap counted in bytes as written (CRLF near the cap included), keys
  it does not know kept by value and byte for byte, copy-on-write, and the
  posture lock never nested with another lock.
* An older pmlens rewriting decisions.yaml and project.yaml leaves
  decision_policy.yaml byte for byte (§5.4), directly and around the tools.
* pm_init's ``decision_posture`` (§3.7): set last and only for a project this
  call created, only while it is unset (decided under the lock); omitted, the
  ``decision_posture_unset`` info; an existing project left byte for byte with
  ``decision_posture_not_changed``; an invalid value refused before anything
  is created; a file it cannot write, a lock it cannot take and a linked
  ``.locks`` turned into warnings while pm_init succeeds.
* pm_update_decision_posture (§3.8): the documented response, unchanged,
  unset and back, the argument checks before any lookup, the file's refusals
  byte for byte, D11 for the reason through the file and both responses, and
  the global ``~/.pm`` (reached through project_path or PM_PROJECT_PATH of the
  home directory) refused before any lock, as §6.3's pm_cleanup row assumes.
* pm_status's ``decision_posture`` (§3.9): the reading table reaching the
  response, a linked file reported by ``symlinks_in_pm_refused``, and, on a
  PM_LENS=1 server, no change tool, nothing written and a
  ``decision_posture_unset`` that points at a full-mode host instead.
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib
import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from pydantic import BaseModel

import pmlens
from pmlens import lineage, policy, storage
from pmlens import server as srv
from pmlens.ledger_errors import LedgerWriteRefused
from pmlens.models import (
    Decision,
    DecisionPosture,
    DecisionStatus,
    Project,
    ProjectNotFoundError,
)
from pmlens.policy import (
    MAX_HISTORY_SCAN,
    MAX_POLICY_BYTES,
    POLICY_FILE,
    POLICY_SCHEMA_UNSUPPORTED,
    POLICY_SECRETS_REDACTED,
    POLICY_TOO_LARGE,
    POLICY_UNREADABLE,
    POSTURE_CHANGED,
    POSTURE_HISTORY_MISMATCH,
    POSTURE_NOT_CHANGED,
    POSTURE_UNKNOWN,
    PolicyView,
    RawPolicy,
    policy_view,
    read_decision_policy,
)
from pmlens.storage import change_decision_posture

NOW = "2026-10-23T02:00:00Z"
SECRET = "AKIA" + "Z" * 16
KEY_BODY = "\n".join(["Zm9v" * 16] * 5)
PRIVATE_KEY = f"-----BEGIN RSA PRIVATE KEY-----\n{KEY_BODY}\n-----END RSA PRIVATE KEY-----"
VIA = "pm_update_decision_posture"


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lineage, "_utc_now", lambda: NOW)


def _path(pm_path: Path) -> Path:
    return pm_path / POLICY_FILE


def _put(pm_path: Path, content: dict | str | bytes) -> Path:
    """Write decision_policy.yaml by hand: a dict as pmlens writes it, text as is."""
    path = _path(pm_path)
    if isinstance(content, dict):
        content = "# PM Lens - decision_policy.yaml\n" + yaml.safe_dump(
            content, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    return path


def _load(pm_path: Path) -> dict:
    return yaml.safe_load(_path(pm_path).read_text(encoding="utf-8"))


def _entry(to: str, previous: str = "unset", reason: str = "", via: str = VIA) -> dict:
    return {"at": NOW, "from": previous, "to": to, "reason": reason, "via": via}


_ONE_ENTRY = object()


def _policy_doc(posture: object = "bold", history: object = _ONE_ENTRY, **extra: object) -> dict:
    """A policy as pmlens writes it; by default one history entry that agrees."""
    doc: dict = {"schema": 1, "decision_posture": posture}
    doc["history"] = [_entry(posture)] if history is _ONE_ENTRY else history
    doc.update(extra)
    return doc


def _codes(view: PolicyView) -> list[str]:
    return [warning["code"] for warning in view.warnings]


def _shown(view: PolicyView) -> str:
    """Everything a reader could show from the view."""
    return json.dumps({"view": view.as_dict(), "warnings": view.warnings}, default=repr)


def _snapshot(root: Path) -> dict[str, object]:
    out: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_symlink():
            out[key] = ("link", os.readlink(path))
        elif path.is_dir():
            out[key] = "dir"
        else:
            out[key] = path.read_bytes()
    return out


def _change(pm_path: Path, to: str, reason: str = "the user asked", **kwargs: object):
    return change_decision_posture(pm_path, to, reason, kwargs.pop("via", VIA), **kwargs)


# ─── The bounded reader shared with lineage.py ───────────────────────────


class TestBoundedReader:
    def test_the_limit_is_inclusive_and_the_lineage_default_is_kept(self, tmp_path: Path):
        path = tmp_path / "f.yaml"
        path.write_bytes(b"a: 1\n# " + b"x" * 3)
        size = path.stat().st_size
        found = lineage.read_bounded_yaml(path, size)
        assert found.exists and not found.unreadable and found.data == {"a": 1}
        assert found.size == size
        over = lineage.read_bounded_yaml(path, size - 1)
        assert over.exists and over.unreadable and over.detail == "larger than the size limit"
        assert lineage._read_bounded(path) == path.read_bytes()  # MAX_LINEAGE_BYTES by default
        with pytest.raises(lineage._NotRegularOrTooLargeError):
            lineage._read_bounded(path, size - 1)

    def test_a_missing_file_is_absent(self, tmp_path: Path):
        found = lineage.read_bounded_yaml(tmp_path / "nothing.yaml", 10)
        assert not found.exists and not found.unreadable and found.data is None

    def test_a_parse_error_names_its_position_not_its_text(self, tmp_path: Path):
        path = tmp_path / "f.yaml"
        path.write_text(f'decision_posture: "{SECRET}\n', encoding="utf-8")
        found = lineage.read_bounded_yaml(path, 1024)
        assert found.unreadable and SECRET not in (found.detail or "")


# ─── Reading: one test per row of the table (S2 spec §2.10) ──────────────


class TestReadingTable:
    def test_no_file_is_unset_and_acts_as_cautious(self, tmp_pm_path: Path):
        before = _snapshot(tmp_pm_path)
        view = read_decision_policy(tmp_pm_path)
        assert view.as_dict() == {
            "value": "unset",
            "effective": "cautious",
            "last_change": None,
            "history_total": 0,
        }
        assert view.warnings == [] and view.state == policy.STATE_MISSING
        assert _snapshot(tmp_pm_path) == before  # no lock, nothing created
        assert not (tmp_pm_path / ".locks").exists()

    def test_a_missing_pm_directory_is_no_file(self, tmp_path: Path):
        view = read_decision_policy(tmp_path / "no-such" / ".pm")
        assert view.value == "unset" and view.warnings == []

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(f'decision_posture: "{SECRET}\n'.encode(), id="yaml-error"),
            pytest.param(b"\xff\xfe not utf-8", id="not-utf-8"),
            pytest.param(b"when: 2026-13-45\n", id="bad-date"),
            pytest.param(b"x: !!python/object:os.system {}\n", id="python-tag"),
            pytest.param(f"- bold\n- {SECRET}\n".encode(), id="a-list"),
            pytest.param(b"bold\n", id="a-scalar"),
            pytest.param(b"", id="empty"),
            pytest.param(
                ("decision_posture: bold\nextra: " + "[" * 70 + "]" * 70 + "\n").encode(),
                id="nested-too-deep",
            ),
        ],
    )
    def test_an_unreadable_file_is_unset_with_a_warning(self, tmp_pm_path: Path, content: bytes):
        _put(tmp_pm_path, content)
        before = _snapshot(tmp_pm_path)
        view = read_decision_policy(tmp_pm_path)
        assert view.value == "unset" and view.effective == "cautious"
        assert view.last_change is None and view.history_total == 0
        assert _codes(view) == [POLICY_UNREADABLE]
        assert view.warnings[0]["level"] == "warning"
        assert view.state == policy.STATE_UNREADABLE
        assert SECRET not in _shown(view)
        assert _snapshot(tmp_pm_path) == before

    def test_a_directory_is_unreadable(self, tmp_pm_path: Path):
        _path(tmp_pm_path).mkdir()
        assert _codes(read_decision_policy(tmp_pm_path)) == [POLICY_UNREADABLE]

    def test_a_link_is_not_followed(self, tmp_pm_path: Path, tmp_path: Path):
        outside = tmp_path / "outside.yaml"
        outside.write_text("decision_posture: bold\n", encoding="utf-8")
        _path(tmp_pm_path).symlink_to(outside)
        view = read_decision_policy(tmp_pm_path)
        assert view.value == "unset" and view.effective == "cautious"
        assert _codes(view) == [POLICY_UNREADABLE]

    def test_the_size_limit_is_inclusive(self, tmp_pm_path: Path):
        head = "decision_posture: bold\nhistory:\n- {to: bold}\n# "
        exact = head + "x" * (MAX_POLICY_BYTES - len(head) - 1) + "\n"
        assert len(exact.encode()) == MAX_POLICY_BYTES
        _put(tmp_pm_path, exact)
        assert read_decision_policy(tmp_pm_path).value == "bold"
        _put(tmp_pm_path, exact + "#")
        assert _codes(read_decision_policy(tmp_pm_path)) == [POLICY_UNREADABLE]

    @pytest.mark.parametrize(
        "kind",
        [
            pytest.param(
                "fifo", marks=pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs")
            ),
            pytest.param(
                "link-to-fifo", marks=pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs")
            ),
            "oversize",
        ],
    )
    def test_a_read_that_could_block_or_grow_returns_within_two_seconds(
        self, tmp_pm_path: Path, tmp_path: Path, kind: str
    ):
        path = _path(tmp_pm_path)
        if kind == "fifo":
            os.mkfifo(path)
        elif kind == "link-to-fifo":
            fifo = tmp_path / "outside.fifo"
            os.mkfifo(fifo)
            path.symlink_to(fifo)
        else:
            with path.open("wb") as handle:
                handle.truncate(512 * 1024 * 1024)  # sparse; nothing is read
        result: list[PolicyView] = []
        reader = threading.Thread(
            target=lambda: result.append(read_decision_policy(tmp_pm_path)), daemon=True
        )
        started = time.monotonic()
        reader.start()
        reader.join(2)
        assert not reader.is_alive(), f"reading a {kind} did not return"
        assert time.monotonic() - started < 2
        assert result[0].value == "unset" and result[0].effective == "cautious"
        assert _codes(result[0]) == [POLICY_UNREADABLE]

    @pytest.mark.parametrize("schema", [2, "1", 1.0, True, None, [1]])
    def test_an_unknown_schema_is_read_as_far_as_it_can_be(self, tmp_pm_path: Path, schema):
        _put(tmp_pm_path, _policy_doc("bold", schema=schema))
        view = read_decision_policy(tmp_pm_path)
        assert view.value == "bold" and view.effective == "bold"
        assert view.last_change["to"] == "bold" and view.history_total == 1
        assert _codes(view) == [POLICY_SCHEMA_UNSUPPORTED]

    @pytest.mark.parametrize("schema", [1, 0, "missing"])
    def test_a_known_or_missing_schema_is_not_flagged(self, tmp_pm_path: Path, schema):
        doc = _policy_doc("cautious")
        if schema == "missing":
            del doc["schema"]
        else:
            doc["schema"] = schema
        _put(tmp_pm_path, doc)
        view = read_decision_policy(tmp_pm_path)
        assert view.value == "cautious" and view.warnings == []

    @pytest.mark.parametrize("posture", ["bold", "cautious"])
    def test_a_known_posture(self, tmp_pm_path: Path, posture: str):
        _put(tmp_pm_path, _policy_doc(posture, history=[_entry(posture, reason="r")]))
        view = read_decision_policy(tmp_pm_path)
        assert view.as_dict() == {
            "value": posture,
            "effective": posture,
            "last_change": _entry(posture, reason="r"),
            "history_total": 1,
        }
        assert view.warnings == [] and view.state == policy.STATE_READ

    @pytest.mark.parametrize("posture", ["missing", None])
    def test_no_posture_is_unset(self, tmp_pm_path: Path, posture: object):
        doc = {"schema": 1, "history": []}
        if posture != "missing":
            doc["decision_posture"] = posture
        _put(tmp_pm_path, doc)
        view = read_decision_policy(tmp_pm_path)
        assert view.value == "unset" and view.effective == "cautious"
        assert view.warnings == []  # unset with no history is the normal state

    def test_an_unknown_string_is_shown_redacted_and_acts_as_cautious(self, tmp_pm_path: Path):
        _put(tmp_pm_path, _policy_doc("exploratory"))
        view = read_decision_policy(tmp_pm_path)
        assert view.value == "exploratory" and view.effective == "cautious"
        assert _codes(view) == [POSTURE_UNKNOWN]  # the history agrees, so no mismatch
        assert "exploratory" not in json.dumps(view.warnings)

    def test_an_unknown_string_with_a_secret_is_redacted_and_cut(self, tmp_pm_path: Path):
        value = f"bold {SECRET} " + "x" * 300
        _put(tmp_pm_path, _policy_doc(value))
        view = read_decision_policy(tmp_pm_path)
        assert SECRET not in _shown(view)
        assert view.value.endswith("…") and len(view.value) == lineage.MAX_LABEL_CHARS + 1
        assert view.effective == "cautious" and view.redactions >= 2  # value and last to
        assert POSTURE_UNKNOWN in _codes(view)

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("decision_posture: !!binary QUtJQVpaWlpaWlpaWlpaWlpaWlo=\n", id="bytes"),
            pytest.param(f"decision_posture: {{nested: {SECRET}}}\n", id="mapping"),
            pytest.param(f"decision_posture: [bold, {SECRET}]\n", id="list"),
            pytest.param("decision_posture: 1\n", id="int"),
            pytest.param("decision_posture: true\n", id="bool"),
            pytest.param("decision_posture: 2026-10-01\n", id="date"),
        ],
    )
    def test_a_value_that_is_not_a_string_is_null_never_stringified(
        self, tmp_pm_path: Path, text: str
    ):
        _put(tmp_pm_path, "schema: 1\n" + text)
        view = read_decision_policy(tmp_pm_path)
        assert view.value is None and view.effective == "cautious"
        # Not a string, so it never agrees with a history either (§2.10).
        assert _codes(view) == [POSTURE_UNKNOWN, POSTURE_HISTORY_MISMATCH]
        shown = _shown(view)
        assert SECRET not in shown and "QUtJQV" not in shown and "nested" not in shown

    @pytest.mark.parametrize(
        ("history", "total", "last_to"),
        [
            pytest.param({"to": "bold"}, 0, None, id="a-mapping"),
            pytest.param("bold", 0, None, id="a-string"),
            pytest.param(None, 0, None, id="null"),
            pytest.param([1, "x", None, ["to", "bold"]], 0, None, id="no-mappings"),
            pytest.param([{"to": "cautious"}, 1, {"to": "bold"}, "x", None], 2, "bold", id="mixed"),
        ],
    )
    def test_only_mappings_of_a_list_count_as_history(
        self, tmp_pm_path: Path, history: object, total: int, last_to: str | None
    ):
        _put(tmp_pm_path, _policy_doc("bold", history=history))
        view = read_decision_policy(tmp_pm_path)
        assert view.history_total == total
        assert (view.last_change or {}).get("to") == last_to
        assert view.value == "bold" and view.effective == "bold"

    def test_history_fields_that_are_not_strings_are_null(self, tmp_pm_path: Path):
        _put(
            tmp_pm_path,
            "decision_posture: bold\nhistory:\n"
            "- at: 2026-10-23T02:00:00Z\n"  # unquoted: a datetime, not a string
            "  from: !!binary QUtJQVpaWlpaWlpaWlpaWlpaWlo=\n"
            "  to: bold\n"
            f"  reason: {{secret: {SECRET}}}\n"
            "  via: [pm_init]\n"
            "  extra: kept out of the view\n",
        )
        view = read_decision_policy(tmp_pm_path)
        assert view.last_change == {
            "at": None,
            "from": None,
            "to": "bold",
            "reason": None,
            "via": None,
        }
        assert view.warnings == []
        shown = _shown(view)
        assert SECRET not in shown and "QUtJQV" not in shown and "extra" not in shown

    def test_the_last_change_is_redacted_and_cut(self, tmp_pm_path: Path):
        reason = f"rotate {SECRET} " + "r" * 300
        long = "v" * 150
        _put(
            tmp_pm_path,
            _policy_doc(
                "bold",
                history=[
                    {
                        "at": long,
                        "from": f"unset {SECRET}",
                        "to": "bold",
                        "reason": reason,
                        "via": long,
                    }
                ],
            ),
        )
        view = read_decision_policy(tmp_pm_path)
        last = view.last_change
        assert SECRET not in _shown(view) and view.redactions == 2
        assert len(last["reason"]) == policy.LAST_REASON_CHARS + 1 and last["reason"][-1] == "…"
        assert last["reason"].startswith("rotate <REDACTED")
        assert last["at"] == "v" * lineage.MAX_LABEL_CHARS + "…"
        assert last["via"] == "v" * lineage.MAX_LABEL_CHARS + "…"
        assert last["from"].startswith("unset <REDACTED")

    def test_a_private_key_in_a_hand_written_reason_is_never_shown(self, tmp_pm_path: Path):
        _put(tmp_pm_path, _policy_doc("bold", history=[_entry("bold", reason=PRIVATE_KEY)]))
        shown = _shown(read_decision_policy(tmp_pm_path))
        assert "Zm9v" not in shown and "PRIVATE KEY" not in shown

    def test_the_history_scan_stops_after_1000_entries_from_the_end(self, tmp_pm_path: Path):
        # 1,500 aliases of one entry: only the last 1,000 are looked at.
        text = (
            "decision_posture: bold\n"
            f"first: &e {{at: '{NOW}', from: unset, to: bold, reason: r, via: pm_init}}\n"
            "history: [" + ", ".join(["*e"] * 1_500) + "]\n"
        )
        _put(tmp_pm_path, text)
        view = read_decision_policy(tmp_pm_path)
        assert view.history_total == MAX_HISTORY_SCAN
        assert view.last_change["to"] == "bold" and view.warnings == []

    def test_a_last_mapping_beyond_the_scan_is_not_seen(self, tmp_pm_path: Path):
        history = [_entry("bold")] + [0] * MAX_HISTORY_SCAN
        _put(tmp_pm_path, _policy_doc("bold", history=history))
        view = read_decision_policy(tmp_pm_path)
        assert view.history_total == 0 and view.last_change is None
        assert _codes(view) == [POSTURE_HISTORY_MISMATCH]  # bold with no history in sight
        history = [_entry("bold")] + [0] * (MAX_HISTORY_SCAN - 1)
        _put(tmp_pm_path, _policy_doc("bold", history=history))
        view = read_decision_policy(tmp_pm_path)
        assert view.history_total == 1 and view.warnings == []

    def test_reading_creates_nothing_and_takes_no_lock(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _put(tmp_pm_path, _policy_doc("bold", history=[]))

        def no_lock(*_args: object, **_kwargs: object):
            raise AssertionError("a reader took a lock")

        monkeypatch.setattr(storage, "_yaml_transaction", no_lock)
        before = _snapshot(tmp_pm_path)
        read_decision_policy(tmp_pm_path)
        assert _snapshot(tmp_pm_path) == before


# ─── The value against the history (S2 spec §2.10) ───────────────────────


_NOT_STR = b"\xff"


@pytest.mark.parametrize(
    ("posture", "history", "mismatch"),
    [
        pytest.param("bold", [_entry("bold")], False, id="agrees"),
        pytest.param("bold", [_entry("cautious")], True, id="differs"),
        pytest.param("bold", [_entry("bold"), _entry("cautious")], True, id="last-differs"),
        pytest.param("bold", [_entry("cautious"), _entry("bold")], False, id="last-agrees"),
        pytest.param("bold", [_entry("bold"), 1, "x"], False, id="last-mapping-agrees"),
        pytest.param("bold", [], True, id="bold-empty-history"),
        pytest.param("bold", "missing", True, id="bold-no-history"),
        pytest.param("bold", [1, None], True, id="bold-no-mapping"),
        pytest.param("unset", [], False, id="unset-empty-history"),
        pytest.param("unset", "missing", False, id="unset-no-history"),
        pytest.param("missing", [], False, id="no-posture-no-history"),
        pytest.param("missing", [_entry("bold")], True, id="no-posture-bold-history"),
        pytest.param("missing", [_entry("unset", "bold")], False, id="no-posture-unset-history"),
        pytest.param("unset", [_entry("unset", "bold")], False, id="unset-agrees"),
        pytest.param("bold", [{"from": "unset", "via": "pm_init"}], True, id="last-to-missing"),
        pytest.param("bold", [{"to": _NOT_STR}], True, id="last-to-not-a-string"),
        pytest.param("exploratory", [_entry("exploratory")], False, id="unknown-agrees"),
        pytest.param("exploratory", [], True, id="unknown-no-history"),
    ],
)
def test_the_value_against_the_last_change(
    tmp_pm_path: Path, posture: str, history: object, mismatch: bool
):
    doc: dict = {"schema": 1}
    if posture != "missing":
        doc["decision_posture"] = posture
    if history != "missing":
        doc["history"] = history
    _put(tmp_pm_path, doc)
    view = read_decision_policy(tmp_pm_path)
    assert (POSTURE_HISTORY_MISMATCH in _codes(view)) is mismatch
    if mismatch:
        warning = next(w for w in view.warnings if w["code"] == POSTURE_HISTORY_MISMATCH)
        assert warning["level"] == "warning"
        for value in ("bold", "cautious", "exploratory"):
            assert value not in warning["message"]  # values are never quoted


def test_the_view_is_pure_and_matches_the_file_reader():
    doc = _policy_doc("cautious", history=[_entry("bold"), _entry("cautious", "bold", "maint")])
    view = policy_view(RawPolicy(exists=True, data=doc))
    assert view.value == "cautious" and view.history_total == 2
    assert view.last_change == _entry("cautious", "bold", "maint")
    assert policy_view(RawPolicy(exists=False)).as_dict()["value"] == "unset"
    unreadable = policy_view(RawPolicy(exists=True, unreadable=True, detail="not a mapping"))
    assert "not a mapping" in unreadable.warnings[0]["message"]


# ─── Writing (S2 spec §3.8) ──────────────────────────────────────────────


class TestChange:
    def test_a_first_change_creates_the_file(self, tmp_pm_path: Path):
        result = _change(tmp_pm_path, "bold", "new product, ambitious is fine")
        assert result.status == "updated" and result.error is None
        assert (result.decision_posture, result.previous, result.history_total) == (
            "bold",
            "unset",
            1,
        )
        assert [w["code"] for w in result.warnings] == [POSTURE_CHANGED]
        assert result.warnings[0]["level"] == "info"
        assert "from unset to bold" in result.warnings[0]["message"]
        text = _path(tmp_pm_path).read_text(encoding="utf-8")
        assert text.startswith("# PM Lens - decision_policy.yaml\n")
        assert _load(tmp_pm_path) == {
            "schema": 1,
            "decision_posture": "bold",
            "history": [_entry("bold", reason="new product, ambitious is fine")],
        }
        assert (tmp_pm_path / ".locks" / "decision_policy.lock").exists()

    def test_round_trip_back_to_unset(self, tmp_pm_path: Path):
        assert _change(tmp_pm_path, "bold").status == "updated"
        result = _change(tmp_pm_path, "unset", "the owner changed")
        assert (result.status, result.previous, result.history_total) == ("updated", "bold", 2)
        view = read_decision_policy(tmp_pm_path)
        assert view.as_dict() == {
            "value": "unset",
            "effective": "cautious",
            "last_change": _entry("unset", "bold", "the owner changed"),
            "history_total": 2,
        }
        assert view.warnings == []
        before = _path(tmp_pm_path).read_bytes()
        again = _change(tmp_pm_path, "unset", "again")
        assert (again.status, again.decision_posture, again.warnings) == ("unchanged", "unset", [])
        assert again.history_total == 2
        assert _path(tmp_pm_path).read_bytes() == before
        assert _change(tmp_pm_path, "cautious").status == "updated"
        assert read_decision_policy(tmp_pm_path).value == "cautious"

    def test_unset_on_a_project_without_the_file_writes_nothing(self, tmp_pm_path: Path):
        result = _change(tmp_pm_path, "unset")
        assert (result.status, result.decision_posture, result.history_total) == (
            "unchanged",
            "unset",
            0,
        )
        assert not _path(tmp_pm_path).exists()

    def test_the_same_value_adds_no_history_even_when_it_disagrees(self, tmp_pm_path: Path):
        _put(tmp_pm_path, _policy_doc("bold", history=[_entry("cautious")]))
        before = _path(tmp_pm_path).read_bytes()
        result = _change(tmp_pm_path, "bold")
        assert result.status == "unchanged" and result.warnings == []
        assert _path(tmp_pm_path).read_bytes() == before

    def test_a_different_value_records_the_current_one_and_resolves_a_mismatch(
        self, tmp_pm_path: Path
    ):
        _put(tmp_pm_path, _policy_doc("bold", history=[]))
        assert POSTURE_HISTORY_MISMATCH in _codes(read_decision_policy(tmp_pm_path))
        result = _change(tmp_pm_path, "cautious", "maintenance from now on")
        assert (result.status, result.previous) == ("updated", "bold")
        assert _load(tmp_pm_path)["history"] == [
            _entry("cautious", "bold", "maintenance from now on")
        ]
        assert read_decision_policy(tmp_pm_path).warnings == []

    @pytest.mark.parametrize("bad", ["exploratory", "Bold", "", "unset ", None, 1])
    def test_an_unknown_value_is_refused_before_anything(self, tmp_pm_path: Path, bad: object):
        before = _snapshot(tmp_pm_path)
        result = change_decision_posture(tmp_pm_path, bad, "r", VIA)  # type: ignore[arg-type]
        assert result.status == "error"
        assert result.error["code"] == policy.INVALID_POSTURE
        assert _snapshot(tmp_pm_path) == before  # no file, no lock

    def test_the_vocabulary_is_the_enum(self):
        assert policy.POSTURES == ("bold", "cautious", "unset")
        assert tuple(DecisionPosture) == ("bold", "cautious", "unset")

    def test_pm_init_style_call_records_an_empty_reason(self, tmp_pm_path: Path):
        result = change_decision_posture(tmp_pm_path, "bold", "", "pm_init", only_if_unset=True)
        assert result.status == "updated"
        assert _load(tmp_pm_path)["history"] == [_entry("bold", via="pm_init")]


class TestOnlyIfUnset:
    def test_it_sets_an_unset_posture_and_leaves_any_other(self, tmp_pm_path: Path):
        first = change_decision_posture(tmp_pm_path, "bold", "", "pm_init", only_if_unset=True)
        assert first.status == "updated"
        before = _path(tmp_pm_path).read_bytes()
        same = change_decision_posture(tmp_pm_path, "bold", "", "pm_init", only_if_unset=True)
        assert (same.status, same.decision_posture, same.warnings) == ("unchanged", "bold", [])
        other = change_decision_posture(tmp_pm_path, "cautious", "", "pm_init", only_if_unset=True)
        assert (other.status, other.decision_posture, other.history_total) == (
            "not_changed",
            "bold",
            1,
        )
        assert other.warnings == [] and other.previous is None
        assert _path(tmp_pm_path).read_bytes() == before
        assert _change(tmp_pm_path, "unset").status == "updated"
        again = change_decision_posture(tmp_pm_path, "cautious", "", "pm_init", only_if_unset=True)
        assert (again.status, again.previous) == ("updated", "unset")

    @pytest.mark.parametrize(
        ("text", "label"),
        [
            ("decision_posture: exploratory\n", "exploratory"),
            ("decision_posture: !!binary QUtJQVpaWlpaWlpaWlpaWlpaWlo=\n", None),
        ],
    )
    def test_an_unknown_value_is_not_unset(self, tmp_pm_path: Path, text: str, label: object):
        _put(tmp_pm_path, text)
        before = _path(tmp_pm_path).read_bytes()
        result = change_decision_posture(tmp_pm_path, "bold", "", "pm_init", only_if_unset=True)
        assert (result.status, result.decision_posture) == ("not_changed", label)
        assert _path(tmp_pm_path).read_bytes() == before

    def test_the_check_reads_the_value_under_the_lock(self, tmp_pm_path: Path):
        # Another writer sets the posture while pm_init waits for the lock:
        # pm_init must see that value, not the unset it would have read before.
        results: list[storage.PostureChangeResult] = []
        errors: list[BaseException] = []

        def pm_init_call() -> None:
            try:
                results.append(
                    change_decision_posture(tmp_pm_path, "bold", "", "pm_init", only_if_unset=True)
                )
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        assert read_decision_policy(tmp_pm_path).value == "unset"
        with storage._yaml_transaction(tmp_pm_path, POLICY_FILE):
            worker = threading.Thread(target=pm_init_call)
            worker.start()
            time.sleep(0.3)
            assert worker.is_alive() and results == []  # waiting for the lock
            doc = _policy_doc("cautious", history=[_entry("cautious", reason="the user said")])
            storage._save_yaml(_path(tmp_pm_path), doc, POLICY_FILE, root=tmp_pm_path)
        worker.join(10)
        assert errors == []
        assert results[0].status == "not_changed"
        assert results[0].decision_posture == "cautious"
        assert _load(tmp_pm_path)["decision_posture"] == "cautious"
        assert len(_load(tmp_pm_path)["history"]) == 1


class TestRefusals:
    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(f'decision_posture: "{SECRET}\n', id="yaml-error"),
            pytest.param(f"- {SECRET}\n", id="a-list"),
            pytest.param("", id="empty"),
            pytest.param(
                "decision_posture: bold\nx: " + "[" * 70 + "]" * 70 + "\n", id="nested-too-deep"
            ),
            pytest.param(f"decision_posture: bold\nhistory: {{to: {SECRET}}}\n", id="history-map"),
            pytest.param("decision_posture: bold\nhistory: bold\n", id="history-string"),
        ],
    )
    def test_an_unreadable_file_is_refused_and_left_alone(self, tmp_pm_path: Path, content: str):
        _put(tmp_pm_path, content)
        before = _path(tmp_pm_path).read_bytes()
        result = _change(tmp_pm_path, "cautious")
        assert result.status == "error"
        assert result.error["code"] == POLICY_UNREADABLE
        assert SECRET not in json.dumps(result.error)
        assert _path(tmp_pm_path).read_bytes() == before

    def test_an_oversized_file_is_refused(self, tmp_pm_path: Path):
        _put(tmp_pm_path, "decision_posture: bold\n# " + "x" * MAX_POLICY_BYTES + "\n")
        before = _path(tmp_pm_path).read_bytes()
        assert _change(tmp_pm_path, "cautious").error["code"] == POLICY_UNREADABLE
        assert _path(tmp_pm_path).read_bytes() == before

    @pytest.mark.parametrize("schema", [2, "1", True])
    def test_an_unknown_schema_is_refused(self, tmp_pm_path: Path, schema: object):
        _put(tmp_pm_path, _policy_doc("bold", schema=schema))
        before = _path(tmp_pm_path).read_bytes()
        result = _change(tmp_pm_path, "cautious")
        assert result.error["code"] == POLICY_SCHEMA_UNSUPPORTED
        assert _path(tmp_pm_path).read_bytes() == before

    def test_a_linked_file_is_refused_and_its_target_left_alone(
        self, tmp_pm_path: Path, tmp_path: Path
    ):
        outside = tmp_path / "outside.yaml"
        outside.write_text("decision_posture: bold\n", encoding="utf-8")
        _path(tmp_pm_path).symlink_to(outside)
        result = _change(tmp_pm_path, "cautious")
        assert result.error["code"] == POLICY_UNREADABLE
        assert outside.read_text(encoding="utf-8") == "decision_posture: bold\n"
        assert _path(tmp_pm_path).is_symlink()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
    def test_a_fifo_is_refused_without_blocking(self, tmp_pm_path: Path):
        os.mkfifo(_path(tmp_pm_path))
        result: list[storage.PostureChangeResult] = []
        worker = threading.Thread(
            target=lambda: result.append(_change(tmp_pm_path, "bold")), daemon=True
        )
        worker.start()
        worker.join(2)
        assert not worker.is_alive(), "the writer blocked on a FIFO"
        assert result[0].error["code"] == POLICY_UNREADABLE

    def test_a_linked_locks_directory_raises_and_writes_nothing(
        self, tmp_pm_path: Path, tmp_path: Path
    ):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (tmp_pm_path / ".locks").symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(LedgerWriteRefused):
            _change(tmp_pm_path, "bold")
        assert not _path(tmp_pm_path).exists()
        assert list(elsewhere.iterdir()) == []

    def test_a_file_swapped_for_a_link_before_the_write_raises(
        self, tmp_pm_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        outside = tmp_path / "outside.yaml"
        outside.write_text("untouched\n", encoding="utf-8")
        real_read = policy.read_policy_raw

        def read_then_swap(pm_path: Path) -> RawPolicy:
            raw = real_read(pm_path)
            _path(pm_path).symlink_to(outside)
            return raw

        monkeypatch.setattr(policy, "read_policy_raw", read_then_swap)
        with pytest.raises(LedgerWriteRefused):
            _change(tmp_pm_path, "bold")
        assert outside.read_text(encoding="utf-8") == "untouched\n"

    @pytest.mark.parametrize(
        "tagged",
        [
            pytest.param("x: !!omap [a: 1, b: 2]\n", id="omap"),
            pytest.param("x: !!pairs [a: 1, a: 2]\n", id="pairs"),
            pytest.param("x: {deep: [!!omap [a: 1]]}\n", id="nested"),
            pytest.param(
                "history:\n- {at: t, from: unset, to: bold, reason: !!pairs [a: 1], via: v}\n",
                id="in-history",
            ),
        ],
    )
    def test_a_value_safe_dump_would_change_is_refused(self, tmp_pm_path: Path, tagged: str):
        # safe_dump writes the tuples of !!omap / !!pairs as plain lists and
        # drops the tag, so the writer refuses rather than change the value.
        _put(tmp_pm_path, "decision_posture: bold\n" + tagged)
        before = _path(tmp_pm_path).read_bytes()
        result = _change(tmp_pm_path, "cautious")
        assert result.status == "error"
        assert result.error["code"] == POLICY_UNREADABLE
        assert "!!omap" in result.error["message"]
        assert _path(tmp_pm_path).read_bytes() == before
        assert policy.read_decision_policy(tmp_pm_path).value == "bold"

    def test_a_set_and_shared_values_are_still_written(self, tmp_pm_path: Path):
        _put(tmp_pm_path, "decision_posture: bold\nx: !!set {a: null}\ny: &s [1]\nz: *s\n")
        assert _change(tmp_pm_path, "cautious").status == "updated"
        doc = _load(tmp_pm_path)
        assert doc["x"] == {"a"} and doc["z"] is doc["y"]

    def test_the_same_value_with_an_omap_is_unchanged(self, tmp_pm_path: Path):
        _put(tmp_pm_path, "decision_posture: bold\nx: !!omap [a: 1]\n")
        assert _change(tmp_pm_path, "bold").status == "unchanged"

    def test_a_value_that_cannot_be_dumped_is_refused_by_type(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _put(tmp_pm_path, _policy_doc("bold"))
        before = _path(tmp_pm_path).read_bytes()

        def failing_dump(*_args: object, **_kwargs: object) -> None:
            raise yaml.representer.RepresenterError(SECRET)

        monkeypatch.setattr(storage.yaml, "safe_dump", failing_dump)
        result = _change(tmp_pm_path, "cautious")
        assert result.error["code"] == POLICY_UNREADABLE
        assert "RepresenterError" in result.error["message"]
        assert SECRET not in result.error["message"]
        assert _path(tmp_pm_path).read_bytes() == before


# ─── Redaction (D11 for the posture, S2 spec §6.2) ───────────────────────


class TestRedaction:
    @pytest.mark.parametrize(
        ("secret", "trace"),
        [(f"key {SECRET} leaked", SECRET), (f"deploy key:\n{PRIVATE_KEY}\nrotate", "Zm9v")],
        ids=["aws-key", "private-key"],
    )
    def test_the_reason_is_redacted_before_saving(self, tmp_pm_path: Path, secret: str, trace):
        result = _change(tmp_pm_path, "bold", secret)
        assert result.status == "updated"
        codes = [w["code"] for w in result.warnings]
        assert codes == [POSTURE_CHANGED, POLICY_SECRETS_REDACTED]
        assert result.warnings[1]["level"] == "warning"
        assert "1 secret-like" in result.warnings[1]["message"]
        written = _path(tmp_pm_path).read_text(encoding="utf-8")
        view = read_decision_policy(tmp_pm_path)
        for shown in (written, json.dumps(result.__dict__, default=repr), _shown(view)):
            assert trace not in shown and "PRIVATE KEY" not in shown
        assert "<REDACTED" in _load(tmp_pm_path)["history"][0]["reason"]

    def test_a_clean_reason_has_no_redaction_warning(self, tmp_pm_path: Path):
        result = _change(tmp_pm_path, "bold", "plain words")
        assert [w["code"] for w in result.warnings] == [POSTURE_CHANGED]

    def test_an_unknown_previous_value_is_recorded_redacted_and_not_quoted(self, tmp_pm_path: Path):
        _put(tmp_pm_path, _policy_doc(f"exploratory {SECRET}", history=[]))
        result = _change(tmp_pm_path, "cautious", "maintenance")
        assert result.status == "updated"
        assert result.previous.startswith("exploratory <REDACTED")
        changed = result.warnings[0]
        assert changed["code"] == POSTURE_CHANGED
        assert "a value pmlens does not recognise" in changed["message"]
        assert "exploratory" not in changed["message"]
        assert [w["code"] for w in result.warnings][1:] == [POLICY_SECRETS_REDACTED]
        assert SECRET not in _path(tmp_pm_path).read_text(encoding="utf-8")
        assert _load(tmp_pm_path)["history"][-1]["from"] == result.previous

    def test_a_previous_value_that_is_not_a_string_is_recorded_as_null(self, tmp_pm_path: Path):
        _put(tmp_pm_path, "decision_posture: !!binary QUtJQVpaWlpaWlpaWlpaWlpaWlo=\n")
        result = _change(tmp_pm_path, "bold", "r")
        assert (result.status, result.previous) == ("updated", None)
        assert _load(tmp_pm_path) == {
            "decision_posture": "bold",
            "history": [_entry("bold", None, "r")],
        }
        assert "QUtJQV" not in _path(tmp_pm_path).read_text(encoding="utf-8")


# ─── Keys the writer does not know, and copy-on-write (S2 spec §2.7, §2.10) ─


_HAND_WRITTEN = """\
# PM Lens - decision_policy.yaml
schema: 1
decision_posture: bold
history: &h
- &first
  at: '2026-10-01T00:00:00Z'
  from: unset
  to: bold
  reason: ''
  via: pm_init
ai_scope:
  allow: [tests, docs]
  blob: !!binary /w==
  when: 2026-10-01
  first: *first
audit: *h
"""


class TestUnknownKeys:
    def test_values_aliases_and_tags_are_kept(self, tmp_pm_path: Path):
        _put(tmp_pm_path, _HAND_WRITTEN)
        result = _change(tmp_pm_path, "cautious", "maintenance")
        assert result.status == "updated" and result.history_total == 2
        doc = _load(tmp_pm_path)
        assert list(doc) == ["schema", "decision_posture", "history", "ai_scope", "audit"]
        assert doc["ai_scope"]["allow"] == ["tests", "docs"]
        assert doc["ai_scope"]["blob"] == b"\xff"
        assert doc["ai_scope"]["when"] == dt.date(2026, 10, 1)
        assert doc["ai_scope"]["first"] is doc["history"][0]  # still one object
        # The key that aliased the history keeps the history as it was: the
        # new entry went to a new list (copy-on-write).
        assert len(doc["audit"]) == 1 and doc["audit"][0] is doc["history"][0]
        assert [e["to"] for e in doc["history"]] == ["bold", "cautious"]

    def test_the_loaded_document_is_not_modified(self):
        loaded = yaml.safe_load(_HAND_WRITTEN)
        history = loaded["history"]
        entry = policy.history_entry(NOW, "bold", "cautious", "r", VIA)
        new = policy.posture_changed_doc(loaded, "cautious", entry)
        assert loaded["decision_posture"] == "bold" and len(history) == 1
        assert loaded["audit"] is history and new["audit"] is history
        assert new["history"] is not history and new["history"][:1] == history
        assert new["ai_scope"] is loaded["ai_scope"]

    @staticmethod
    def _section(pm_path: Path, key: str) -> bytes:
        """The bytes of one top-level key of the file, its line included."""
        lines = _path(pm_path).read_bytes().splitlines(keepends=True)
        start = next(n for n, line in enumerate(lines) if line.startswith(f"{key}:".encode()))
        end = next(
            (n for n in range(start + 1, len(lines)) if lines[n][:1] not in (b" ", b"-")),
            len(lines),
        )
        return b"".join(lines[start:end])

    def test_unknown_keys_keep_their_bytes_through_later_changes(self, tmp_pm_path: Path):
        _put(
            tmp_pm_path,
            _policy_doc(
                "bold",
                ai_scope={"allow": ["tests", "docs"], "blob": b"\xff", "deep": {"a": [1, 2]}},
                note="日本語の注記",
            ),
        )
        assert _change(tmp_pm_path, "cautious", "first").status == "updated"
        sections = {key: self._section(tmp_pm_path, key) for key in ("ai_scope", "note")}
        assert b"!!binary" in sections["ai_scope"]
        for to in ("unset", "bold", "cautious"):
            assert _change(tmp_pm_path, to, f"to {to}").status == "updated"
            for key, section in sections.items():
                assert self._section(tmp_pm_path, key) == section
        assert read_decision_policy(tmp_pm_path).history_total == 5


# ─── Size cap, counted in bytes as written (S2 spec §2.8) ────────────────


_PAD_REASON = "p" * 500


def _filled(entries: int) -> dict:
    """A policy whose history holds ``entries`` changes, ending at bold."""
    history = []
    for n in range(entries):
        to = "bold" if n % 2 == 0 else "cautious"
        previous = "unset" if n == 0 else ("cautious" if to == "bold" else "bold")
        history.append(_entry(to, previous, _PAD_REASON))
    history[-1]["to"] = "bold"
    return {"schema": 1, "decision_posture": "bold", "history": history}


def _after(doc: dict, reason: str) -> str:
    """The text a change of ``doc`` to cautious with ``reason`` writes (LF newlines)."""
    entry = policy.history_entry(NOW, "bold", "cautious", reason, VIA)
    new = policy.posture_changed_doc(doc, "cautious", entry)
    text = storage._dump_yaml_capped(new, POLICY_FILE, 10 * MAX_POLICY_BYTES)
    assert text is not None
    return text


class TestSizeCap:
    @pytest.mark.parametrize("linesep", ["\n", "\r\n"], ids=["lf", "crlf"])
    def test_a_change_accepted_at_the_cap_reads_back(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch, linesep: str
    ):
        monkeypatch.setattr(os, "linesep", linesep)

        def size(doc: dict, reason: str) -> int:
            return storage.written_size(_after(doc, reason))

        # The most entries that leave room for a one-character reason, then a
        # reason that lands exactly on the cap (one plain line, one byte a char).
        entries = 1
        while size(_filled(entries + 1), "r") <= MAX_POLICY_BYTES:
            entries += 1
        doc = _filled(entries)
        exact = "r" * (MAX_POLICY_BYTES - size(doc, "r") + 1)
        assert len(exact) <= policy.MAX_REASON_CHARS
        assert size(doc, exact) == MAX_POLICY_BYTES

        path = _path(tmp_pm_path)
        storage._save_yaml(path, doc, POLICY_FILE, root=tmp_pm_path)
        before = path.read_bytes()
        refused = _change(tmp_pm_path, "cautious", exact + "r")
        assert refused.status == "error"
        assert refused.error["code"] == POLICY_TOO_LARGE
        assert "remediation" in refused.error
        assert path.read_bytes() == before
        if linesep == "\r\n":
            # Counted in LF bytes, the refused change would have fitted.
            lf_text = _after(doc, exact + "r")
            assert len(lf_text.encode("utf-8")) <= MAX_POLICY_BYTES

        accepted = _change(tmp_pm_path, "cautious", exact)
        assert accepted.status == "updated", accepted.error
        data = path.read_bytes()
        assert len(data) == MAX_POLICY_BYTES
        assert (b"\r\n" in data) is (linesep == "\r\n")
        view = read_decision_policy(tmp_pm_path)
        assert view.state == policy.STATE_READ and view.warnings == []
        assert view.value == "cautious" and view.history_total == entries + 1

    def test_a_string_repeated_through_aliases_counts_in_full(self, tmp_pm_path: Path):
        # PyYAML anchors a shared list or mapping but writes a string out at
        # every alias, so a 30 KB file would be written back as megabytes.
        text = (
            "decision_posture: bold\nhistory: []\n"
            f"s: &s {'x' * 1_000}\n"
            "many: [" + ", ".join(["*s"] * 5_000) + "]\n"
        )
        _put(tmp_pm_path, text)
        assert read_decision_policy(tmp_pm_path).state == policy.STATE_READ
        before = _path(tmp_pm_path).read_bytes()
        started = time.monotonic()
        result = _change(tmp_pm_path, "cautious")
        assert time.monotonic() - started < 2
        assert result.error["code"] == POLICY_TOO_LARGE
        assert _path(tmp_pm_path).read_bytes() == before

    def test_shared_lists_stay_anchors_and_fit(self, tmp_pm_path: Path):
        nest = ["a0: &a0 [" + ", ".join(["x" * 50] * 9) + "]"]
        nest += [f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 9) + "]" for i in range(1, 7)]
        _put(tmp_pm_path, "decision_posture: bold\nhistory: []\n" + "\n".join(nest) + "\n")
        result = _change(tmp_pm_path, "cautious")
        assert result.status == "updated"
        assert _path(tmp_pm_path).stat().st_size < 4_000


# ─── The posture lock is never nested (S2 spec §5.3) ─────────────────────


class TestLocks:
    def test_the_writer_takes_the_posture_lock_alone(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        taken: list[tuple[str, list[str]]] = []
        real = storage._yaml_transaction

        @contextmanager
        def spy(base_dir: Path, filename: str, **kwargs: object) -> Iterator[None]:
            held = [label for label, _ in storage._held_ranked_locks()]
            taken.append((filename, held))
            with real(base_dir, filename, **kwargs):
                yield

        monkeypatch.setattr(storage, "_yaml_transaction", spy)
        for to in ("bold", "bold", "unset"):
            _change(tmp_pm_path, to)
        change_decision_posture(tmp_pm_path, "cautious", "", "pm_init", only_if_unset=True)
        assert taken == [(POLICY_FILE, [])] * 4

    def test_it_finishes_while_other_threads_hold_the_decision_locks(self, tmp_pm_path: Path):
        holding = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with storage._yaml_transaction(tmp_pm_path, "decisions.yaml"):
                with storage._yaml_transaction(tmp_pm_path, "decision_lineage-ADR-001"):
                    holding.set()
                    release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert holding.wait(5)
            started = time.monotonic()
            result = _change(tmp_pm_path, "bold")
            assert time.monotonic() - started < 2
            assert result.status == "updated"
        finally:
            release.set()
            holder.join(10)

    def test_no_other_function_takes_the_posture_lock(self):
        # Only change_decision_posture takes it, once, so no writer can take
        # it inside another lock and it takes no other lock inside it.
        package = Path(pmlens.__file__).parent
        takers: list[tuple[str, str]] = []
        for module in ("storage.py", "server.py", "policy.py", "lineage.py"):
            tree = ast.parse((package / module).read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if not (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "_yaml_transaction"
                        and len(node.args) > 1
                    ):
                        continue
                    label = ast.unparse(node.args[1])
                    if "POLICY_FILE" in label or "decision_policy" in label:
                        takers.append((module, fn.name))
        assert takers == [("storage.py", "change_decision_posture")]
        writer = next(
            fn
            for fn in ast.walk(ast.parse((package / "storage.py").read_text(encoding="utf-8")))
            if isinstance(fn, ast.FunctionDef) and fn.name == "change_decision_posture"
        )
        calls = [
            node
            for node in ast.walk(writer)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_yaml_transaction"
        ]
        assert len(calls) == 1


# ─── An older pmlens rewriting the other ledgers (S2 spec §5.4) ──────────


class _OldProject(BaseModel):
    """project.yaml as pmlens 0.16.0 modelled it: unknown keys dropped on rewrite."""

    name: str
    status: str = "development"


def _older_rewrite(pm_path: Path) -> None:
    """Rewrite project.yaml and decisions.yaml as pmlens 0.16.0 would (unknown keys dropped)."""
    for name, rewrite in (
        ("project.yaml", lambda data: _OldProject(**data).model_dump(mode="json")),
        (
            "decisions.yaml",
            lambda data: {
                "decisions": [
                    {k: d[k] for k in ("id", "title", "date", "status")} for d in data["decisions"]
                ]
            },
        ),
    ):
        path = pm_path / name
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        path.write_text(
            f"# PM Lens - {name}\n" + yaml.safe_dump(rewrite(data), sort_keys=False),
            encoding="utf-8",
        )


def test_an_older_rewrite_of_decisions_and_project_keeps_the_policy(tmp_pm_path: Path):
    storage._save_project(tmp_pm_path, Project(name="p"))
    storage.add_decision(
        tmp_pm_path,
        Decision(
            id="ADR-001", title="t", date=dt.date(2026, 10, 1), status=DecisionStatus.PROPOSED
        ),
    )
    assert _change(tmp_pm_path, "bold", "new product").status == "updated"
    before = _path(tmp_pm_path).read_bytes()
    _older_rewrite(tmp_pm_path)
    storage.add_decision(tmp_pm_path, Decision(id="ADR-002", title="next"))
    assert _path(tmp_pm_path).read_bytes() == before
    assert read_decision_policy(tmp_pm_path).value == "bold"


def test_the_tools_around_an_older_rewrite_keep_the_policy(tmp_path: Path):
    # The same through the tools: a posture set by pm_init and changed with
    # the change tool survives an older pmlens rewriting the other ledgers,
    # and pm_init on the project afterwards leaves it byte for byte.
    root = tmp_path / "proj"
    root.mkdir()
    srv.pm_init(project_path=str(root), decision_posture="bold")
    srv.pm_add_decision(title="t", context="c", decision="d", project_path=str(root))
    srv.pm_update_decision_posture(
        decision_posture="cautious", reason="maintenance", project_path=str(root)
    )
    pm_path = root / ".pm"
    before = _path(pm_path).read_bytes()
    _older_rewrite(pm_path)
    srv.pm_init(project_path=str(root))
    srv.pm_add_decision(title="next", context="c", decision="d", project_path=str(root))
    assert _path(pm_path).read_bytes() == before
    posture = srv.pm_status(project_path=str(root))["decision_posture"]
    assert (posture["value"], posture["history_total"]) == ("cautious", 2)


# ─── The tools: pm_init (S2 spec §3.7) ───────────────────────────────────

_TOOL = "pm_update_decision_posture"
_UNSET_VIEW = {"value": "unset", "effective": "cautious", "last_change": None, "history_total": 0}


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    return root


def _init(root: Path, **kwargs: object) -> dict:
    return srv.pm_init(project_path=str(root), **kwargs)


def _update(root: Path, posture: object, reason: object = "the user asked") -> dict:
    return srv.pm_update_decision_posture(
        decision_posture=posture,  # type: ignore[arg-type]
        reason=reason,  # type: ignore[arg-type]
        project_path=str(root),
    )


def _status(root: Path) -> dict:
    return srv.pm_status(project_path=str(root))


def _result_codes(result: dict) -> list[str]:
    return [w["code"] for w in result.get("warnings", [])]


def _posture_codes(result: dict) -> list[str]:
    """The codes of a response's warnings that concern the posture or its file."""
    return [
        code
        for code in _result_codes(result)
        if code.startswith(("decision_posture", "decision_policy"))
    ]


def _warning(result: dict, code: str) -> dict:
    found = [w for w in result.get("warnings", []) if w["code"] == code]
    assert len(found) == 1, (code, _result_codes(result))
    return found[0]


def _ledger_snapshot(pm_path: Path) -> dict[str, object]:
    """Everything in .pm but the lock files (a writer creates its lock file)."""
    return {k: v for k, v in _snapshot(pm_path).items() if not k.startswith(".locks")}


def _bytes_or_none(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


class TestPmInit:
    @pytest.mark.parametrize("posture", ["bold", "cautious"])
    def test_a_new_project_gets_the_posture_the_user_chose(self, tmp_path: Path, posture: str):
        root = _root(tmp_path)
        result = _init(root, decision_posture=posture)
        assert result["status"] == "initialized"
        assert result["decision_posture"] == {
            "value": posture,
            "effective": posture,
            "last_change": _entry(posture, via="pm_init"),
            "history_total": 1,
        }
        assert _posture_codes(result) == [POSTURE_CHANGED]
        changed = _warning(result, POSTURE_CHANGED)
        assert changed["level"] == "info"
        assert f"from unset to {posture}" in changed["message"]
        # The server never writes a reason on the user's behalf.
        assert _load(root / ".pm") == {
            "schema": 1,
            "decision_posture": posture,
            "history": [_entry(posture, via="pm_init")],
        }

    def test_omitted_leaves_it_unset_and_asks_for_it(self, tmp_path: Path):
        root = _root(tmp_path)
        result = _init(root)
        assert result["decision_posture"] == _UNSET_VIEW
        assert _posture_codes(result) == [policy.POSTURE_UNSET]
        unset = _warning(result, policy.POSTURE_UNSET)
        assert unset["level"] == "info"
        assert "ask them" in unset["message"]
        for text in ("bold", "cautious", "acts as cautious", _TOOL):
            assert text in unset["remediation"], text
        assert not _path(root / ".pm").exists()

    @pytest.mark.parametrize("bad", ["unset", "exploratory", "Bold", "", SECRET, 1, ["bold"]])
    def test_an_invalid_posture_creates_nothing(self, tmp_path: Path, bad: object):
        root = _root(tmp_path)
        registry = tmp_path / "fake_global_pm"
        before = (_snapshot(root), _snapshot(registry))
        result = _init(root, decision_posture=bad)
        assert result["status"] == "error"
        assert result["code"] == policy.INVALID_POSTURE
        assert result["field"] == "decision_posture"
        assert "bold or cautious" in result["message"]
        assert SECRET not in json.dumps(result)
        assert (_snapshot(root), _snapshot(registry)) == before
        assert not (root / ".pm").exists()

    def test_the_posture_is_set_last(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        root = _root(tmp_path)
        seen: list[tuple[bool, bool, list[str]]] = []
        real = srv.change_decision_posture

        def spy(pm_path: Path, *args: object, **kwargs: object) -> object:
            seen.append(
                (
                    (pm_path / "project.yaml").exists(),
                    (root / "CLAUDE.md").exists(),
                    [entry.path for entry in storage.load_registry().projects],
                )
            )
            return real(pm_path, *args, **kwargs)

        monkeypatch.setattr(srv, "change_decision_posture", spy)
        assert _init(root, decision_posture="bold")["decision_posture"]["value"] == "bold"
        assert seen == [(True, True, [str(root.resolve())])]

    @pytest.mark.parametrize("state", ["no file", "unset again", "cautious", "unknown"])
    def test_an_existing_project_keeps_its_posture(self, tmp_path: Path, state: str):
        root = _root(tmp_path)
        _init(root)
        pm_path = root / ".pm"
        if state == "unset again":
            assert _update(root, "bold")["status"] == "updated"
            assert _update(root, "unset", "the owner left")["status"] == "updated"
        elif state == "cautious":
            assert _update(root, "cautious")["status"] == "updated"
        elif state == "unknown":
            _put(pm_path, _policy_doc("exploratory"))
        before = _bytes_or_none(_path(pm_path))
        result = _init(root, decision_posture="bold")
        assert result["status"] == "initialized"
        assert _bytes_or_none(_path(pm_path)) == before
        assert result["decision_posture"]["value"] != "bold"
        not_changed = _warning(result, policy.POSTURE_NOT_CHANGED)
        assert not_changed["level"] == "info"
        assert "only for a new project" in not_changed["message"]
        assert _TOOL in not_changed["remediation"]
        assert policy.POSTURE_CHANGED not in _result_codes(result)
        # The user chose a posture, so pm_init does not also ask for one.
        assert policy.POSTURE_UNSET not in _result_codes(result)

    def test_an_existing_project_with_that_posture_already_gets_no_notice(self, tmp_path: Path):
        root = _root(tmp_path)
        _init(root, decision_posture="bold")
        before = _path(root / ".pm").read_bytes()
        result = _init(root, decision_posture="bold")
        assert _posture_codes(result) == []
        assert _path(root / ".pm").read_bytes() == before

    def test_reinitializing_without_a_posture_changes_nothing(self, tmp_path: Path):
        root = _root(tmp_path)
        _init(root, decision_posture="cautious")
        before = _path(root / ".pm").read_bytes()
        result = _init(root)
        assert _posture_codes(result) == []
        assert result["decision_posture"]["value"] == "cautious"
        assert _path(root / ".pm").read_bytes() == before

    # A repository can ship .pm/decision_policy.yaml without project.yaml, so
    # a project pm_init creates may already have a posture.

    def _shipped(self, tmp_path: Path, content: dict | str) -> Path:
        root = _root(tmp_path)
        (root / ".pm").mkdir()
        _put(root / ".pm", content)
        return root

    def test_a_shipped_posture_of_the_same_value_is_left_without_a_notice(self, tmp_path: Path):
        root = self._shipped(tmp_path, _policy_doc("bold"))
        before = _path(root / ".pm").read_bytes()
        result = _init(root, decision_posture="bold")
        assert _posture_codes(result) == []
        assert _path(root / ".pm").read_bytes() == before

    def test_a_shipped_posture_of_another_value_is_not_changed(self, tmp_path: Path):
        root = self._shipped(tmp_path, _policy_doc("cautious"))
        before = _path(root / ".pm").read_bytes()
        result = _init(root, decision_posture="bold")
        assert _posture_codes(result) == [policy.POSTURE_NOT_CHANGED]
        assert "already sets" in _warning(result, policy.POSTURE_NOT_CHANGED)["message"]
        assert _path(root / ".pm").read_bytes() == before
        assert result["decision_posture"]["value"] == "cautious"

    def test_a_shipped_unset_posture_is_set_and_its_history_kept(self, tmp_path: Path):
        shipped = _policy_doc("unset", history=[_entry("bold"), _entry("unset", "bold")])
        root = self._shipped(tmp_path, shipped)
        result = _init(root, decision_posture="cautious")
        assert _posture_codes(result) == [POSTURE_CHANGED]
        assert _load(root / ".pm")["history"] == [
            _entry("bold"),
            _entry("unset", "bold"),
            _entry("cautious", via="pm_init"),
        ]

    @pytest.mark.parametrize(
        ("content", "code"),
        [
            ("decision_posture: [unclosed\n", POLICY_UNREADABLE),
            ("- not\n- a mapping\n", POLICY_UNREADABLE),
            (_policy_doc("unset", history=[], schema=2), POLICY_SCHEMA_UNSUPPORTED),
            ("decision_posture: unset\nx: !!omap [a: 1]\n", POLICY_UNREADABLE),
        ],
        ids=["yaml-error", "not-a-mapping", "newer-schema", "omap"],
    )
    def test_a_shipped_file_that_cannot_be_written_is_a_warning(
        self, tmp_path: Path, content: dict | str, code: str
    ):
        root = self._shipped(tmp_path, content)
        before = _path(root / ".pm").read_bytes()
        result = _init(root, decision_posture="bold")
        assert result["status"] == "initialized"
        assert (root / ".pm" / "project.yaml").exists()
        assert _posture_codes(result) == [code]  # reported once, not by both paths
        warning = _warning(result, code)
        assert warning["level"] == "warning"
        # A second pm_init finds project.yaml and does not set the posture, so
        # the way on is the change tool, never "try again" (S2 spec §3.7).
        assert "try again" not in warning["message"]
        assert _TOOL in warning["remediation"]
        assert _path(root / ".pm").read_bytes() == before
        assert POSTURE_NOT_CHANGED in _posture_codes(_init(root, decision_posture="bold"))

    def test_a_lock_that_cannot_be_taken_is_a_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PM_LOCK_TIMEOUT_S", "0.2")
        root = _root(tmp_path)
        pm_path = storage.init_pm_directory(root)  # no project.yaml: still a new project
        holding = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with storage._yaml_transaction(pm_path, POLICY_FILE, timeout=5):
                holding.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert holding.wait(5)
            result = _init(root, decision_posture="bold")
        finally:
            release.set()
            holder.join(10)
        assert result["status"] == "initialized"
        assert (pm_path / "project.yaml").exists()
        assert not _path(pm_path).exists()
        assert result["decision_posture"] == _UNSET_VIEW
        assert _posture_codes(result) == [policy.POSTURE_NOT_SET]
        not_set = _warning(result, policy.POSTURE_NOT_SET)
        assert not_set["level"] == "warning"
        assert _TOOL in not_set["remediation"]
        # A fixed text: the exception's own words never reach the response.
        assert "timeout after" not in json.dumps(result)

    def test_a_locks_directory_linked_meanwhile_is_a_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # .pm/.locks becomes a link after project.yaml was written (another
        # process, or a checkout): the posture's lock is refused, and pm_init
        # still succeeds.
        import pmlens.claudemd

        root = _root(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        real = pmlens.claudemd.ensure_claudemd

        def then_link_locks(project_root: Path, *args: object, **kwargs: object) -> object:
            done = real(project_root, *args, **kwargs)
            locks = project_root / ".pm" / ".locks"
            for child in locks.iterdir():
                child.unlink()
            locks.rmdir()
            locks.symlink_to(outside, target_is_directory=True)
            return done

        monkeypatch.setattr(pmlens.claudemd, "ensure_claudemd", then_link_locks)
        result = _init(root, decision_posture="bold")
        assert result["status"] == "initialized"
        assert _posture_codes(result) == [policy.POSTURE_NOT_SET]
        assert ".locks" in _warning(result, "symlinks_in_pm_refused")["message"]
        assert not _path(root / ".pm").exists()
        assert list(outside.iterdir()) == []
        assert "refus" not in _warning(result, policy.POSTURE_NOT_SET)["message"]

    def test_an_existing_project_with_a_linked_locks_directory_still_initializes(
        self, tmp_path: Path
    ):
        root = _root(tmp_path)
        _init(root)
        outside = tmp_path / "outside"
        outside.mkdir()
        locks = root / ".pm" / ".locks"
        for child in locks.iterdir():
            child.unlink()
        locks.rmdir()
        locks.symlink_to(outside, target_is_directory=True)
        result = _init(root, decision_posture="bold")
        assert result["status"] == "initialized"
        assert ".locks" in _warning(result, "symlinks_in_pm_refused")["message"]
        assert _posture_codes(result) == [policy.POSTURE_NOT_CHANGED]
        assert list(outside.iterdir()) == []

    def test_the_unset_check_is_made_under_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Another writer sets the posture after pm_init created project.yaml
        # and before pm_init sets its own: pm_init must not overwrite it.
        import pmlens.claudemd

        root = _root(tmp_path)
        real = pmlens.claudemd.ensure_claudemd

        def meanwhile(project_root: Path, *args: object, **kwargs: object) -> object:
            done = real(project_root, *args, **kwargs)
            assert _change(project_root / ".pm", "cautious", "set elsewhere").status == "updated"
            return done

        monkeypatch.setattr(pmlens.claudemd, "ensure_claudemd", meanwhile)
        result = _init(root, decision_posture="bold")
        assert _posture_codes(result) == [policy.POSTURE_NOT_CHANGED]
        assert result["decision_posture"]["value"] == "cautious"
        assert _load(root / ".pm")["history"] == [_entry("cautious", reason="set elsewhere")]

    def test_a_linked_policy_file_is_named_and_left_alone(self, tmp_path: Path):
        root = _root(tmp_path)
        (root / ".pm").mkdir()
        target = tmp_path / "elsewhere.yaml"
        target.write_text("decision_posture: unset\n", encoding="utf-8")
        _path(root / ".pm").symlink_to(target)
        result = _init(root, decision_posture="bold")
        assert result["status"] == "initialized"
        assert POLICY_FILE in _warning(result, "symlinks_in_pm_refused")["message"]
        assert _posture_codes(result) == [POLICY_UNREADABLE]
        assert target.read_text(encoding="utf-8") == "decision_posture: unset\n"
        assert _path(root / ".pm").is_symlink()


# ─── The tools: pm_update_decision_posture (S2 spec §3.8) ────────────────


class TestChangeTool:
    def test_a_change_returns_the_documented_shape(self, tmp_path: Path):
        root = _root(tmp_path)
        _init(root, decision_posture="bold")
        result = _update(root, "cautious", "maintenance after the launch")
        assert result == {
            "status": "updated",
            "decision_posture": "cautious",
            "previous": "bold",
            "history_total": 2,
            "warnings": [
                {
                    "level": "info",
                    "code": "decision_posture_changed",
                    "message": (
                        "The project's decision posture changed from bold to cautious. pmlens "
                        "cannot tell who asked for it; tell the user."
                    ),
                }
            ],
        }
        assert _load(root / ".pm")["history"][-1] == _entry(
            "cautious", "bold", "maintenance after the launch"
        )

    def test_the_same_value_is_unchanged_and_adds_no_history(self, tmp_path: Path):
        root = _root(tmp_path)
        _init(root, decision_posture="bold")
        before = _path(root / ".pm").read_bytes()
        result = _update(root, "bold", "still new work")
        assert result == {
            "status": "unchanged",
            "decision_posture": "bold",
            "previous": None,
            "history_total": 1,
            "warnings": [],
        }
        assert _path(root / ".pm").read_bytes() == before

    def test_back_to_unset_and_on_again(self, tmp_path: Path):
        root = _root(tmp_path)
        _init(root, decision_posture="bold")
        assert _update(root, "unset", "a new owner")["previous"] == "bold"
        status = _status(root)
        assert status["decision_posture"] == {
            "value": "unset",
            "effective": "cautious",
            "last_change": _entry("unset", "bold", "a new owner"),
            "history_total": 2,
        }
        assert policy.POSTURE_UNSET in _result_codes(status)
        # pm_init on the existing project does not set it; the tool does.
        assert policy.POSTURE_NOT_CHANGED in _result_codes(_init(root, decision_posture="bold"))
        assert _update(root, "bold", "the new owner wants it")["status"] == "updated"
        assert _status(root)["decision_posture"]["history_total"] == 3

    @pytest.mark.parametrize(
        ("posture", "reason", "code"),
        [
            ("exploratory", "r", policy.INVALID_POSTURE),
            (SECRET, "r", policy.INVALID_POSTURE),
            ("", "r", policy.INVALID_POSTURE),
            (None, "r", policy.INVALID_POSTURE),
            (["bold"], "r", policy.INVALID_POSTURE),
            ("bold", "", "reason_required"),
            ("bold", "  \n", "reason_required"),
            ("bold", None, "reason_required"),
            ("bold", 5, "reason_required"),
            ("bold", "r" * (policy.MAX_REASON_CHARS + 1), "text_too_long"),
        ],
    )
    def test_argument_errors_come_first_and_write_nothing(
        self, tmp_path: Path, posture: object, reason: object, code: str
    ):
        root = _root(tmp_path)
        _init(root, decision_posture="bold")
        before = _snapshot(root / ".pm")
        result = _update(root, posture, reason)
        assert result["status"] == "error" and result["code"] == code
        assert result["field"] == (
            "decision_posture" if code == policy.INVALID_POSTURE else "reason"
        )
        assert SECRET not in json.dumps(result)
        assert _snapshot(root / ".pm") == before
        # Checked before the project is looked up.
        missing = srv.pm_update_decision_posture(
            decision_posture=posture,  # type: ignore[arg-type]
            reason=reason,  # type: ignore[arg-type]
            project_path=str(tmp_path / "no-such-project"),
        )
        assert missing["code"] == code

    def test_a_reason_at_the_limit_is_accepted(self, tmp_path: Path):
        root = _root(tmp_path)
        _init(root)
        reason = "r" * policy.MAX_REASON_CHARS
        assert _update(root, "bold", reason)["status"] == "updated"
        assert _load(root / ".pm")["history"][-1]["reason"] == reason

    @pytest.mark.parametrize(
        ("content", "code"),
        [
            ("decision_posture: [unclosed\n", POLICY_UNREADABLE),
            (_policy_doc("bold", history={"not": "a list"}), POLICY_UNREADABLE),
            (_policy_doc("bold", schema=2), POLICY_SCHEMA_UNSUPPORTED),
            ("full", POLICY_TOO_LARGE),
        ],
        ids=["unreadable", "history-not-a-list", "newer-schema", "too-large"],
    )
    def test_a_file_it_must_not_rewrite_is_refused_byte_for_byte(
        self, tmp_path: Path, content: dict | str, code: str
    ):
        root = _root(tmp_path)
        _init(root)
        pm_path = root / ".pm"
        reason = "the user asked"
        if content == "full":
            # The most history that fits, then the longest reason allowed.
            entries = 1
            while storage.written_size(_after(_filled(entries + 1), "r")) <= MAX_POLICY_BYTES:
                entries += 1
            content, reason = _filled(entries), "r" * policy.MAX_REASON_CHARS
        _put(pm_path, content)
        before = _ledger_snapshot(pm_path)
        result = _update(root, "cautious", reason)
        assert result["status"] == "error" and result["code"] == code
        assert _ledger_snapshot(pm_path) == before

    def test_secrets_in_the_reason_are_never_saved_or_shown(self, tmp_path: Path):
        # D11 for the posture: the file, the change's response and pm_status.
        root = _root(tmp_path)
        _init(root)
        result = _update(root, "bold", f"token {SECRET}\n{PRIVATE_KEY}")
        assert result["status"] == "updated"
        redacted = _warning(result, POLICY_SECRETS_REDACTED)
        assert redacted["level"] == "warning"
        assert redacted["message"].startswith("2 secret-like")
        status = _status(root)
        written = _path(root / ".pm").read_text(encoding="utf-8")
        for shown in (json.dumps(result), json.dumps(status), written):
            assert SECRET not in shown
            assert "BEGIN RSA PRIVATE KEY" not in shown
            assert KEY_BODY.splitlines()[0] not in shown
        assert "<REDACTED" in status["decision_posture"]["last_change"]["reason"]

    def test_a_hand_written_secret_is_never_shown_by_pm_status(self, tmp_path: Path):
        root = _root(tmp_path)
        _init(root)
        _put(
            root / ".pm",
            _policy_doc(
                f"x {SECRET}",
                history=[_entry(f"x {SECRET}", reason=f"by hand {SECRET} " + "y" * 400)],
            ),
        )
        status = _status(root)
        assert SECRET not in json.dumps(status)
        posture = status["decision_posture"]
        assert posture["value"].startswith("x <REDACTED")
        assert len(posture["last_change"]["reason"]) <= policy.LAST_REASON_CHARS + 1
        assert POSTURE_UNKNOWN in _result_codes(status)

    @pytest.mark.parametrize("route", ["project_path", "PM_PROJECT_PATH"])
    def test_the_home_directory_global_pm_is_never_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
    ):
        # The resolver takes any directory holding a .pm for an explicit
        # project_path or PM_PROJECT_PATH, and ~/.pm (the registry) is one.
        # decision_policy.yaml is written only to a project's .pm, which is
        # why pm_cleanup does not look for it in ~/.pm (S2 spec §6.3).
        home = _global_home(tmp_path, monkeypatch)
        before = _snapshot(home / ".pm")
        if route == "PM_PROJECT_PATH":
            monkeypatch.setenv("PM_PROJECT_PATH", str(home))
            monkeypatch.chdir(tmp_path)
            target: str | None = None
        else:
            target = str(home)
        with pytest.raises(ProjectNotFoundError) as caught:
            srv.pm_update_decision_posture(
                decision_posture="bold", reason="the user asked", project_path=target
            )
        assert str(home) not in str(caught.value)
        assert _snapshot(home / ".pm") == before
        assert not (home / ".pm" / ".locks").exists()


def _global_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory whose .pm is the global one, holding only the registry."""
    home = tmp_path / "home"
    (home / ".pm").mkdir(parents=True)
    (home / ".pm" / "registry.yaml").write_text("projects: []\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(storage, "GLOBAL_PM_DIR", home / ".pm")
    monkeypatch.setattr(srv, "GLOBAL_PM_DIR", home / ".pm")
    return home


class TestGlobalPmRefused:
    @pytest.mark.parametrize("only_if_unset", [False, True])
    def test_the_writer_refuses_the_global_pm_before_any_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, only_if_unset: bool
    ):
        home = _global_home(tmp_path, monkeypatch)
        before = _snapshot(home / ".pm")
        with pytest.raises(ProjectNotFoundError):
            change_decision_posture(
                home / ".pm", "bold", "", "pm_init", only_if_unset=only_if_unset
            )
        assert _snapshot(home / ".pm") == before

    @pytest.mark.parametrize("which", ["global_pm_dir", "home"])
    def test_either_name_for_the_global_pm_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
    ):
        # GLOBAL_PM_DIR is fixed at import; $HOME is read at call time.
        home = tmp_path / "home"
        (home / ".pm").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        if which == "home":
            monkeypatch.setenv("HOME", str(home))
        else:
            monkeypatch.setenv("HOME", str(elsewhere))
            monkeypatch.setattr(storage, "GLOBAL_PM_DIR", home / ".pm")
        # A path that only resolves to it (a link to the home directory).
        alias = tmp_path / "alias"
        alias.symlink_to(home, target_is_directory=True)
        with pytest.raises(ProjectNotFoundError):
            _change(alias / ".pm", "bold")
        assert not _path(home / ".pm").exists()

    @pytest.mark.parametrize("route", ["writer", "project_path", "PM_PROJECT_PATH"])
    def test_a_case_variant_of_the_home_directory_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
    ):
        # On a case-insensitive file system (macOS's default APFS) another
        # spelling of $HOME names the same directory, and resolve() keeps the
        # spelling it was given, so comparing resolved paths alone let it past.
        home = _global_home(tmp_path, monkeypatch)
        variant = home.parent / home.name.upper()
        if not variant.exists():
            pytest.skip("the file system is case-sensitive")
        assert variant.resolve() != home.resolve()
        before = _snapshot(home / ".pm")
        with pytest.raises(ProjectNotFoundError) as caught:
            if route == "writer":
                change_decision_posture(variant / ".pm", "bold", "", "pm_init")
            else:
                target: str | None = str(variant)
                if route == "PM_PROJECT_PATH":
                    monkeypatch.setenv("PM_PROJECT_PATH", str(variant))
                    monkeypatch.chdir(tmp_path)
                    target = None
                srv.pm_update_decision_posture(
                    decision_posture="bold", reason="the user asked", project_path=target
                )
        assert str(home) not in str(caught.value)
        assert str(variant) not in str(caught.value)
        assert _snapshot(home / ".pm") == before
        assert not (home / ".pm" / ".locks").exists()
        assert not _path(home / ".pm").exists()

    def test_a_project_under_the_home_directory_is_still_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        home = _global_home(tmp_path, monkeypatch)
        root = home / "work" / "proj"
        root.mkdir(parents=True)
        _init(root)
        assert _update(root, "bold")["status"] == "updated"
        assert _load(root / ".pm")["decision_posture"] == "bold"
        assert not _path(home / ".pm").exists()


# ─── The tools: pm_status (S2 spec §3.9) ─────────────────────────────────


def _project(tmp_pm_path: Path) -> Path:
    storage._save_project(tmp_pm_path, Project(name="p"))
    return tmp_pm_path.parent


class TestPmStatus:
    def test_no_file_is_unset_and_reading_creates_nothing(self, tmp_pm_path: Path):
        root = _project(tmp_pm_path)
        before = _snapshot(tmp_pm_path)
        status = _status(root)
        assert status["decision_posture"] == _UNSET_VIEW
        assert _posture_codes(status) == [policy.POSTURE_UNSET]
        unset = _warning(status, policy.POSTURE_UNSET)
        assert set(unset) == {"level", "code", "message", "remediation"}
        assert _TOOL in unset["remediation"]
        assert _snapshot(tmp_pm_path) == before

    def test_the_value_and_the_last_change_are_shown(self, tmp_pm_path: Path):
        root = _project(tmp_pm_path)
        _update(root, "bold", "a new product")
        _update(root, "cautious", "本番公開の後は保守フェーズとして扱う")
        status = _status(root)
        assert status["decision_posture"] == {
            "value": "cautious",
            "effective": "cautious",
            "last_change": _entry("cautious", "bold", "本番公開の後は保守フェーズとして扱う"),
            "history_total": 2,
        }
        assert _posture_codes(status) == []

    @pytest.mark.parametrize(
        ("content", "value", "codes"),
        [
            (_policy_doc("bold"), "bold", []),
            (_policy_doc("exploratory"), "exploratory", [POSTURE_UNKNOWN]),
            (_policy_doc({"a": 1}), None, [POSTURE_UNKNOWN, POSTURE_HISTORY_MISMATCH]),
            (_policy_doc("bold", history=[]), "bold", [POSTURE_HISTORY_MISMATCH]),
            (_policy_doc("bold", schema=2), "bold", [POLICY_SCHEMA_UNSUPPORTED]),
            ("decision_posture: [unclosed\n", "unset", [POLICY_UNREADABLE]),
        ],
        ids=["bold", "unknown", "not-a-string", "mismatch", "newer-schema", "unreadable"],
    )
    def test_the_reading_table_reaches_the_response(
        self, tmp_pm_path: Path, content: dict | str, value: object, codes: list[str]
    ):
        root = _project(tmp_pm_path)
        _put(tmp_pm_path, content)
        status = _status(root)
        assert status["decision_posture"]["value"] == value
        assert _posture_codes(status) == codes  # an unreadable file is not "unset" too
        for code in codes:
            assert set(_warning(status, code)) == {"level", "code", "message", "remediation"}
            assert _warning(status, code)["level"] == "warning"
        assert status["decision_posture"]["effective"] == (
            "bold" if value == "bold" else "cautious"
        )

    def test_a_linked_policy_file_is_reported_and_not_followed(self, tmp_pm_path: Path):
        root = _project(tmp_pm_path)
        target = tmp_pm_path.parent / "elsewhere.yaml"
        target.write_text("decision_posture: bold\n", encoding="utf-8")
        _path(tmp_pm_path).symlink_to(target)
        status = _status(root)
        assert status["decision_posture"]["value"] == "unset"
        assert POLICY_FILE in _warning(status, "symlinks_in_pm_refused")["message"]
        assert _posture_codes(status) == [POLICY_UNREADABLE]


def _reload_server() -> None:
    """Re-import server.py so PM_LENS is read again (as tests/test_lens_mode.py does)."""
    srv._memory_stores.clear()
    srv.REGISTERED_TOOLS.clear()
    importlib.reload(srv)


@pytest.fixture
def lens_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setenv("PM_LENS", "1")
    _reload_server()
    try:
        yield srv
    finally:
        monkeypatch.delenv("PM_LENS", raising=False)
        _reload_server()


class TestLens:
    def test_the_change_tool_is_not_registered(self, lens_server: ModuleType):
        assert lens_server.PM_LENS_ENABLED is True
        assert "pm_status" in lens_server.REGISTERED_TOOLS
        assert _TOOL not in lens_server.REGISTERED_TOOLS
        assert "pm_init" not in lens_server.REGISTERED_TOOLS

    def test_pm_status_writes_nothing_and_never_names_the_change_tool(
        self, tmp_pm_path: Path, lens_server: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        root = _project(tmp_pm_path)
        before = _snapshot(tmp_pm_path)
        status = lens_server.pm_status(project_path=str(root))
        assert _snapshot(tmp_pm_path) == before
        assert status["decision_posture"] == _UNSET_VIEW
        unset = _warning(status, policy.POSTURE_UNSET)
        assert _TOOL not in json.dumps(status)
        assert "full-mode pmlens host" in unset["remediation"]
        # The message (ask the user, the two choices) is the same in full mode;
        # only the remediation's last sentence differs.
        monkeypatch.setattr(lens_server, "PM_LENS_ENABLED", False)
        full = lens_server._posture_unset_notice()
        assert _TOOL in full["remediation"]
        assert unset["message"] == full["message"]
        shared = full["remediation"].split(" Once ")[0]
        assert unset["remediation"].startswith(shared + " Once ")

    def test_a_set_posture_reads_the_same_under_lens(
        self, tmp_pm_path: Path, lens_server: ModuleType
    ):
        root = _project(tmp_pm_path)
        _change(tmp_pm_path, "bold", "new product")
        before = _snapshot(tmp_pm_path)
        status = lens_server.pm_status(project_path=str(root))
        assert _snapshot(tmp_pm_path) == before
        assert status["decision_posture"]["value"] == "bold"
        assert status["decision_posture"]["last_change"] == _entry("bold", reason="new product")
        assert _posture_codes(status) == []
