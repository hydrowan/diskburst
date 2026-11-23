"""Clone dedupe on real APFS files in tmp_path: planning, accounting, verified apply, resume."""

import gzip
import json
import os
import subprocess
import sys
import time

import pytest

from diskburst import bulk_listing
from diskburst.clone_dedupe.__main__ import main
from diskburst.clone_dedupe.accounting import account
from diskburst.clone_dedupe.apply import ApplyLimits, run_apply
from diskburst.clone_dedupe.inventory import canonical_roots
from diskburst.clone_dedupe.planner import build_plan
from diskburst.clone_dedupe.plan_io import write_plan
from diskburst.clone_dedupe import xattr
from diskburst.clone_dedupe.replace import temp_path_for

FILE_BYTES = 256 * 1024
OLD_NS = time.time_ns() - 72 * 3600 * 1_000_000_000
QUARANTINE = "com.apple.quarantine"


@pytest.fixture(autouse=True)
def require_apfs(tmp_path):
    if sys.platform != "darwin" or not bulk_listing.AVAILABLE:
        pytest.skip("APFS clones need macOS")
    probe = tmp_path / "probe"
    probe.write_bytes(b"x")
    if subprocess.run(["cp", "-c", probe, tmp_path / "probe2"], capture_output=True).returncode:
        pytest.skip("tmp dir is not on a clone-capable (APFS) volume")
    probe.unlink(), (tmp_path / "probe2").unlink()


def put(path, data: bytes, mtime_ns: int = OLD_NS):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def clone(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-c", "-p", src, dst], check=True)
    return dst


def attrs(path) -> dict:
    """cloneid, private bytes and clone refcount from a bulk listing of the parent."""
    entry = next(e for e in bulk_listing.list_directory(str(path.parent)) if e[1] == path.name)
    return {"cloneid": entry[8], "private": entry[7], "refcnt": entry[9]}


def plan_and_write(root, plan_path, **kw):
    roots = canonical_roots([str(r) for r in (root if isinstance(root, list) else [root])])
    plan = build_plan(roots, kw.get("min_size", 1024), kw.get("min_age_hours", 48), 1, time.time_ns())
    write_plan(str(plan_path), {"created_at_ns": time.time_ns(), "roots": roots}, plan.records)
    return plan


def apply(plan_path, **kw):
    kw.setdefault("min_free_bytes", 0)  # the dev volume may itself be near full
    return run_apply(str(plan_path), f"{plan_path}.log", ApplyLimits(**kw))


@pytest.fixture
def dupes(tmp_path):
    root = tmp_path / "root"
    data = os.urandom(FILE_BYTES)
    put(root / "a/keep.bin", data, OLD_NS - 10)
    copy = put(root / "b/copy.bin", data)
    xattr.put(copy, QUARANTINE, b"0081;00000000;test;")
    os.chmod(copy, 0o640)
    os.utime(copy, ns=(OLD_NS, OLD_NS))
    return root


def test_duplicate_becomes_clone_with_metadata_preserved(dupes, tmp_path):
    copy, keep = dupes / "b/copy.bin", dupes / "a/keep.bin"
    before = os.lstat(copy)
    plan = plan_and_write(dupes, tmp_path / "plan.gz")
    assert [r["path"] for r in plan.records] == [str(copy.resolve())]
    assert apply(tmp_path / "plan.gz")["replaced"] == 1
    after = os.lstat(copy)
    assert copy.read_bytes() == keep.read_bytes()
    assert attrs(copy)["cloneid"] == attrs(keep)["cloneid"] and attrs(copy)["refcnt"] == 2
    assert (after.st_mtime_ns, after.st_mode) == (before.st_mtime_ns, before.st_mode)
    assert xattr.get(str(copy), QUARANTINE) == b"0081;00000000;test;"
    assert QUARANTINE not in xattr.list_names(str(keep))
    assert attrs(copy)["private"] == 0


def test_accounting_frees_private_size(dupes, tmp_path):
    copy = dupes / "b/copy.bin"
    private = attrs(copy)["private"]
    totals = account(plan_and_write(dupes, tmp_path / "plan.gz"))["total"]
    assert totals["logical_bytes"] == FILE_BYTES and totals["freed_bytes"] == private > 0
    apply(tmp_path / "plan.gz")
    assert attrs(copy)["private"] == 0


def test_file_changed_after_plan_is_skipped(dupes, tmp_path):
    copy = dupes / "b/copy.bin"
    plan_and_write(dupes, tmp_path / "plan.gz")
    st = os.lstat(copy)
    with open(copy, "r+b") as f:
        f.write(b"changed")
    os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns))
    counts = apply(tmp_path / "plan.gz")
    assert counts["skipped"] == 1 and counts["replaced"] == 0
    assert copy.read_bytes()[:7] == b"changed"
    log = [json.loads(line) for line in open(f"{tmp_path / 'plan.gz'}.log")]
    assert log[-1]["reason"] == "content_differs"
    assert not os.path.exists(temp_path_for(str(copy)))


def test_mtime_change_is_skipped_before_cloning(dupes, tmp_path):
    plan_and_write(dupes, tmp_path / "plan.gz")
    os.utime(dupes / "b/copy.bin")
    counts = apply(tmp_path / "plan.gz")
    assert counts["skipped"] == 1


def test_recent_and_xattr_files_are_skipped(tmp_path):
    root, data = tmp_path / "root", os.urandom(FILE_BYTES)
    put(root / "a.bin", data)
    put(root / "recent.bin", data, time.time_ns())
    odd = put(root / "odd.bin", data)
    xattr.put(odd, "user.test", b"1")
    plan = plan_and_write(root, tmp_path / "plan.gz")
    assert plan.records == []
    assert plan.skips.reasons["modified_within_min_age"][0] == 1
    assert plan.skips.reasons["unusual_metadata"][0] == 1
    assert plan.skips.xattrs["user.test"] == 1


def test_hard_link_with_outside_name_is_skipped(tmp_path):
    root, data = tmp_path / "root", os.urandom(FILE_BYTES)
    keep = put(root / "a/keep.bin", data)
    for name in ("k2.bin", "k3.bin"):
        os.link(keep, root / "a" / name)
    linked = put(root / "b/linked.bin", data)
    (tmp_path / "outside").mkdir()
    os.link(linked, tmp_path / "outside/linked.bin")
    plan = plan_and_write(root, tmp_path / "plan.gz")
    assert plan.records == []
    assert plan.skips.reasons["hardlink_names_outside_roots"][0] == 1
    assert account(plan)["total"]["held_by_hardlinks_outside_roots"] > 0


def test_hard_link_fully_inside_replaces_every_name(tmp_path):
    root, data = tmp_path / "root", os.urandom(FILE_BYTES)
    keep = put(root / "a/keep.bin", data)
    for name in ("k2.bin", "k3.bin"):
        os.link(keep, root / "a" / name)
    linked = put(root / "b/linked.bin", data)
    os.link(linked, root / "b/linked2.bin")
    plan = plan_and_write(root, tmp_path / "plan.gz")
    assert sorted(os.path.basename(r["path"]) for r in plan.records) == ["linked.bin", "linked2.bin"]
    assert account(plan)["total"]["freed_bytes"] == os.lstat(linked).st_blocks * 512
    assert apply(tmp_path / "plan.gz")["replaced"] == 2
    assert os.lstat(root / "b/linked.bin").st_nlink == 1
    assert attrs(root / "b/linked2.bin")["cloneid"] == attrs(root / "b/linked.bin")["cloneid"]


def test_existing_clone_family_is_one_physical_copy(tmp_path):
    root, data = tmp_path / "root", os.urandom(FILE_BYTES)
    clone(put(root / "a/orig.bin", data), root / "b/clone.bin")
    assert plan_and_write(root, tmp_path / "plan.gz").groups == 0
    put(root / "c/copy.bin", data)
    plan = plan_and_write(root, tmp_path / "plan.gz")
    totals = account(plan)["total"]
    assert [os.path.basename(r["path"]) for r in plan.records] == ["copy.bin"]
    assert (totals["physical_copies"], totals["logical_bytes"]) == (1, FILE_BYTES)


def test_clone_family_member_outside_root_holds_blocks(tmp_path):
    root, data = tmp_path / "root", os.urandom(FILE_BYTES)
    keep = put(root / "a/keep.bin", data)
    for name in ("c1.bin", "c2.bin"):
        clone(keep, root / "a" / name)
    shared = put(root / "b/shared.bin", data)
    clone(shared, tmp_path / "outside/shared.bin")
    totals = account(plan_and_write(root, tmp_path / "plan.gz"))["total"]
    assert totals["freed_bytes"] == 0
    assert totals["held_by_clones_outside_roots"] == os.lstat(shared).st_blocks * 512


def test_clone_family_fully_inside_is_freed(tmp_path):
    root, data = tmp_path / "root", os.urandom(FILE_BYTES)
    keep = put(root / "a/keep.bin", data)
    for name in ("c1.bin", "c2.bin"):
        clone(keep, root / "a" / name)
    clone(put(root / "b/x.bin", data), root / "b/y.bin")
    totals = account(plan_and_write(root, tmp_path / "plan.gz"))["total"]
    assert totals["replacements"] == 2
    assert totals["freed_bytes"] == os.lstat(root / "b/x.bin").st_blocks * 512


def test_symlinks_are_not_followed(tmp_path):
    root, data = tmp_path / "root", os.urandom(FILE_BYTES)
    put(root / "a.bin", data)
    put(tmp_path / "outside/b.bin", data)
    os.symlink(tmp_path / "outside", root / "link_dir")
    os.symlink(tmp_path / "outside/b.bin", root / "link_file.bin")
    plan = plan_and_write(root, tmp_path / "plan.gz")
    assert plan.records == [] and plan.walk.symlinks == 2


def test_resume_skips_completed_and_cleans_stale_temp(tmp_path):
    root, data = tmp_path / "root", os.urandom(FILE_BYTES)
    put(root / "a/keep.bin", data, OLD_NS - 10)
    for i in range(3):
        put(root / f"b/copy{i}.bin", data)
    plan_and_write(root, tmp_path / "plan.gz")
    assert apply(tmp_path / "plan.gz", max_files=1)["replaced"] == 1
    stale = temp_path_for(str((root / "b/copy2.bin").resolve()))
    put(type(root)(stale), b"leftover")
    counts = apply(tmp_path / "plan.gz")
    assert (counts["already_done"], counts["replaced"]) == (1, 2)
    assert not os.path.exists(stale)
    ids = {attrs(root / f"b/copy{i}.bin")["cloneid"] for i in range(3)}
    assert ids == {attrs(root / "a/keep.bin")["cloneid"]}


def test_limit_bytes_and_free_space_floor_stop(dupes, tmp_path):
    plan_and_write(dupes, tmp_path / "plan.gz")
    assert apply(tmp_path / "plan.gz", max_bytes=FILE_BYTES - 1)["stopped:limit_bytes"] == 1
    assert apply(tmp_path / "plan.gz", min_free_bytes=1 << 62)["stopped:free_space_below_floor"] == 1
    assert (dupes / "b/copy.bin").stat().st_ino == json.loads(
        gzip.open(tmp_path / "plan.gz", "rt").readlines()[1])["inode"]


def test_apply_refuses_old_plan_and_missing_plan(dupes, tmp_path):
    roots = canonical_roots([str(dupes)])
    write_plan(str(tmp_path / "old.gz"), {"created_at_ns": OLD_NS, "roots": roots}, [])
    with pytest.raises(SystemExit, match="old"):
        apply(tmp_path / "old.gz")
    with pytest.raises(SystemExit, match="--plan"):
        main(["--apply"])


def test_cli_dry_run_writes_plan_and_summaries_without_touching_files(dupes, tmp_path, capsys):
    before = {p: os.lstat(p) for p in dupes.rglob("*")}
    (tmp_path / "out").mkdir()
    main([str(dupes), "--plan-out", str(tmp_path / "out/plan.jsonl.gz"), "--workers", "2"])
    after = {p: os.lstat(p) for p in dupes.rglob("*")}
    assert {p: (s.st_ino, s.st_mtime_ns, s.st_ctime_ns) for p, s in before.items()} == {
        p: (s.st_ino, s.st_mtime_ns, s.st_ctime_ns) for p, s in after.items()
    }
    summary = json.load(open(tmp_path / "out/plan.summary.json"))
    assert summary["total"]["replacements"] == 1
    assert "(b) really freed" in capsys.readouterr().out


def test_sampled_verification_mode(dupes, tmp_path):
    plan_and_write(dupes, tmp_path / "plan.gz")
    assert apply(tmp_path / "plan.gz", verify="sampled")["replaced"] == 1
