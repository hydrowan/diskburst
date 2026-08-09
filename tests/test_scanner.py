"""Scanner unit tests on a synthetic on-disk fixture (a few KB, no downloads)."""

import os
import time

import pytest

from diskburst.scanner import BLOCK_BYTES, Scanner
from diskburst.serialize import serialize_view


def allocated(path: str) -> int:
    return os.lstat(path).st_blocks * BLOCK_BYTES


@pytest.fixture
def fixture_tree(tmp_path):
    """tmp/{a/{big.bin,small.txt}, a/sub/nested.bin, b/other.bin, link -> a}"""
    (tmp_path / "a" / "sub").mkdir(parents=True)
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "big.bin").write_bytes(b"x" * 20_000)
    (tmp_path / "a" / "small.txt").write_bytes(b"y" * 100)
    (tmp_path / "a" / "sub" / "nested.bin").write_bytes(b"z" * 5_000)
    (tmp_path / "b" / "other.bin").write_bytes(b"w" * 3_000)
    os.symlink(tmp_path / "a", tmp_path / "link")  # must not be followed
    return tmp_path


def scan(root: str, **kwargs) -> Scanner:
    scanner = Scanner([root], workers=4, **kwargs)
    scanner.start()
    assert scanner.done.wait(timeout=10), "scan did not finish"
    return scanner


def test_totals_match_lstat(fixture_tree):
    scanner = scan(str(fixture_tree))
    root = scanner.roots[str(fixture_tree)]
    files = ["a/big.bin", "a/small.txt", "a/sub/nested.bin", "b/other.bin"]
    dirs = ["", "a", "a/sub", "b"]
    expected = sum(allocated(str(fixture_tree / f)) for f in files)
    expected += sum(
        allocated(str(fixture_tree / d)) if d else 0 for d in dirs[1:]
    )  # child dir entries counted, root's own entry not
    assert root.total_bytes == expected
    assert root.total_files == 4
    assert scanner.stats.errors == 0


def test_symlinks_not_followed(fixture_tree):
    scanner = scan(str(fixture_tree))
    root = scanner.roots[str(fixture_tree)]
    assert "link" not in root.children
    assert root.total_files == 4  # nothing double-counted through the symlink


def test_hardlinks_counted_once(fixture_tree):
    os.link(fixture_tree / "a" / "big.bin", fixture_tree / "b" / "hardlink.bin")
    scanner = scan(str(fixture_tree))
    root = scanner.roots[str(fixture_tree)]
    assert root.total_files == 4  # 5 dir entries, 4 distinct files


def test_big_file_retention_threshold(fixture_tree):
    scanner = scan(str(fixture_tree), big_file_bytes=10_000)
    a = scanner.roots[str(fixture_tree)].children["a"]
    assert [name for name, _, _ in a.big_files] == ["big.bin"]
    assert all(mtime > 0 for _, _, mtime in a.big_files)


def test_unreadable_dir_flagged(fixture_tree):
    locked = fixture_tree / "locked"
    locked.mkdir()
    (locked / "hidden.bin").write_bytes(b"h" * 1_000)
    locked.chmod(0o000)
    try:
        scanner = scan(str(fixture_tree))
        assert scanner.stats.errors >= 1
        assert scanner.roots[str(fixture_tree)].children["locked"].error
    finally:
        locked.chmod(0o755)


def test_serialize_view_prunes_and_sorts(fixture_tree):
    scanner = scan(str(fixture_tree), big_file_bytes=1_000)
    view = serialize_view(scanner, str(fixture_tree), depth=4)
    names = [c["name"] for c in view["children"]]
    assert names[0] == "a"  # sorted by size desc
    sizes = [c["size"] for c in view["children"]]
    assert sizes == sorted(sizes, reverse=True)
    a = next(c for c in view["children"] if c["name"] == "a")
    assert {c["name"] for c in a["children"]} >= {"big.bin", "sub"}


def test_find_resolves_nested_path(fixture_tree):
    scanner = scan(str(fixture_tree))
    node, path = scanner.find(str(fixture_tree / "a" / "sub"))
    assert node.name == "sub"
    assert path.endswith("a/sub")
    assert scanner.find(str(fixture_tree / "nope")) is None


def test_largest_files_global(fixture_tree):
    scanner = scan(str(fixture_tree), big_file_bytes=1_000)
    largest = scanner.largest_files(n=10)
    assert largest[0][0].endswith("big.bin")
    sizes = [s for _, s, _ in largest]
    assert sizes == sorted(sizes, reverse=True)


def test_scan_terminates_quickly(fixture_tree):
    start = time.monotonic()
    scan(str(fixture_tree))
    assert time.monotonic() - start < 5


def test_largest_files_scoped_to_path(fixture_tree):
    scanner = scan(str(fixture_tree), big_file_bytes=1_000)
    scoped = scanner.largest_files(n=10, path=str(fixture_tree / "b"))
    assert [p.split("/")[-1] for p, _, _ in scoped] == ["other.bin"]
    assert scanner.largest_files(n=10, path=str(fixture_tree / "nope")) == []


def test_cache_round_trip(fixture_tree, monkeypatch, tmp_path_factory):
    from diskburst import cache

    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path_factory.mktemp("cache")))
    scanner = scan(str(fixture_tree), big_file_bytes=1_000)
    cache.save_cache(scanner)
    loaded = cache.load_cache([str(fixture_tree)], 1_000, workers=4)
    assert loaded is not None
    assert loaded.done.is_set()
    assert loaded.cache_age is not None
    original = scanner.roots[str(fixture_tree)]
    restored = loaded.roots[str(fixture_tree)]
    assert restored.total_bytes == original.total_bytes
    assert restored.total_files == original.total_files
    assert loaded.largest_files(n=5) == scanner.largest_files(n=5)


def test_cache_miss_returns_none(monkeypatch, tmp_path_factory):
    from diskburst import cache

    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path_factory.mktemp("empty")))
    assert cache.load_cache(["/nonexistent-root"], 1_000, workers=4) is None


def test_diff_detects_growth_and_removal(fixture_tree, monkeypatch, tmp_path_factory):
    from diskburst import cache
    from diskburst.diff import diff_against_snapshot

    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path_factory.mktemp("cache")))
    monkeypatch.setattr("diskburst.diff.MIN_DELTA_BYTES", 1_000)
    first = scan(str(fixture_tree), big_file_bytes=1_000)
    cache.save_cache(first)  # becomes 'latest'
    (fixture_tree / "a" / "grown.bin").write_bytes(b"g" * 50_000)
    (fixture_tree / "b" / "other.bin").unlink()
    second = scan(str(fixture_tree), big_file_bytes=1_000)
    cache.save_cache(second)  # rotates first into history
    result = diff_against_snapshot(second, which="prev")
    assert result["available"]
    by_path = {e["path"]: e for e in result["entries"]}
    grown = str(fixture_tree / "a" / "grown.bin")
    removed = str(fixture_tree / "b" / "other.bin")
    assert by_path[grown]["kind"] == "file-added" and by_path[grown]["delta"] > 0
    assert by_path[removed]["kind"] == "file-removed" and by_path[removed]["delta"] < 0


def test_diff_lists_snapshots_and_accepts_stamp(fixture_tree, monkeypatch, tmp_path_factory):
    from diskburst import cache
    from diskburst.diff import diff_against_snapshot

    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path_factory.mktemp("cache")))
    monkeypatch.setattr("diskburst.diff.MIN_DELTA_BYTES", 1_000)
    first = scan(str(fixture_tree), big_file_bytes=1_000)
    cache.save_cache(first)
    (fixture_tree / "a" / "grown.bin").write_bytes(b"g" * 50_000)
    second = scan(str(fixture_tree), big_file_bytes=1_000)
    cache.save_cache(second)
    result = diff_against_snapshot(second, which="prev")
    assert result["available"] and len(result["snapshots"]) == 1
    stamp = result["snapshots"][0]["stamp"]
    assert result["baseline_stamp"] == stamp
    by_stamp = diff_against_snapshot(second, which=str(stamp))
    assert by_stamp["entries"] == result["entries"]


def test_diff_ignores_trash_additions(fixture_tree, monkeypatch, tmp_path_factory):
    from diskburst import cache
    from diskburst.diff import diff_against_snapshot

    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path_factory.mktemp("cache")))
    monkeypatch.setattr("diskburst.diff.MIN_DELTA_BYTES", 1_000)
    first = scan(str(fixture_tree), big_file_bytes=1_000)
    cache.save_cache(first)
    trash = fixture_tree / ".Trash"
    trash.mkdir()
    (trash / "moved_here.bin").write_bytes(b"t" * 50_000)
    second = scan(str(fixture_tree), big_file_bytes=1_000)
    cache.save_cache(second)
    result = diff_against_snapshot(second, which="prev")
    assert result["available"]
    assert not any(".Trash" in e["path"] for e in result["entries"])


def test_duplicates_ignore_trash(fixture_tree, monkeypatch):
    from diskburst import dupes

    monkeypatch.setattr(dupes, "MIN_DUPE_BYTES", 10_000)
    trash = fixture_tree / ".Trash"
    trash.mkdir()
    (trash / "trashed_copy.bin").write_bytes(b"x" * 20_000)  # identical to a/big.bin
    scanner = scan(str(fixture_tree), big_file_bytes=10_000)
    result = dupes.find_duplicates(scanner)
    assert "trashed_copy.bin" not in str(result["groups"])


def test_duplicates_by_fingerprint(fixture_tree, monkeypatch):
    from diskburst import dupes

    monkeypatch.setattr(dupes, "MIN_DUPE_BYTES", 10_000)
    (fixture_tree / "b" / "copy.bin").write_bytes(b"x" * 20_000)  # same content as a/big.bin
    (fixture_tree / "b" / "samesize.bin").write_bytes(b"q" * 20_000)  # same size, diff bytes
    scanner = scan(str(fixture_tree), big_file_bytes=10_000)
    result = dupes.find_duplicates(scanner)
    assert len(result["groups"]) == 1
    group = result["groups"][0]
    names = {p.split("/")[-1] for p in group["files"]}
    assert names == {"big.bin", "copy.bin"}
    assert "samesize.bin" not in str(group["files"])
    assert result["wasted"] == group["size"]
    assert all(cmd.startswith("rm ") and "ln -s" in cmd for cmd in group["commands"])


def test_filter_matchers():
    from diskburst.filter import FilterError, build_matcher

    ext = build_matcher("exact", "", ["JPG", ".dng"])
    assert ext("photo.JPG") and ext("shot.dng") and not ext("model.pt")
    fuzzy = build_matcher("fuzzy", "sftnsr", [])
    assert fuzzy("model.safetensors") and not fuzzy("archive.zip")
    rx = build_matcher("regex", r"^flux.*\.bin$", [])
    assert rx("flux-dev-00001.bin") and not rx("notflux.bin")
    both = build_matcher("exact", "big", ["bin"])  # text AND ext compose
    assert both("big.bin") and not both("big.txt") and not both("small.bin")
    with pytest.raises(FilterError):
        build_matcher("regex", "([unclosed", [])


def test_filtered_view_sizes(fixture_tree):
    from diskburst.filter import TreeFilter

    scanner = scan(str(fixture_tree), big_file_bytes=1_000)
    flt = TreeFilter(scanner, "exact", "", ["bin"])
    root = scanner.roots[str(fixture_tree)]
    expected = sum(
        allocated(str(fixture_tree / f))
        for f in ["a/big.bin", "a/sub/nested.bin", "b/other.bin"]
    )
    assert flt.size(root) == expected
    assert flt.count(root) == 3
    view = serialize_view(scanner, str(fixture_tree), depth=4, flt=flt)
    assert view["size"] == expected
    names = {c["name"] for c in view["children"]}
    assert "a" in names and "b" in names
    txt_flt = TreeFilter(scanner, "exact", "", ["txt"])
    txt_view = serialize_view(scanner, str(fixture_tree), depth=4, flt=txt_flt)
    child_names = {c["name"] for c in txt_view.get("children", [])}
    assert "b" not in child_names  # no matching files under b — dropped from the map


def test_largest_files_with_matcher(fixture_tree):
    from diskburst.filter import build_matcher

    scanner = scan(str(fixture_tree), big_file_bytes=1_000)
    match = build_matcher("exact", "", ["txt"])
    files = scanner.largest_files(n=10, match=match)
    assert [p.split("/")[-1] for p, _, _ in files] == ["small.txt"]


def test_config_precedence_cli_over_file(tmp_path):
    from diskburst.cli import resolve_args

    cfg = tmp_path / "config.toml"
    cfg.write_text('port = 7777\nworkers = 8\nroots = ["/tmp"]\ncache = false\n')
    args = resolve_args(["--config", str(cfg)])
    assert (args.port, args.workers, args.roots, args.no_cache) == (7777, 8, ["/tmp"], True)
    args = resolve_args(["--config", str(cfg), "--port", "9999"])
    assert args.port == 9999  # CLI beats config
    assert args.workers == 8  # untouched keys still come from config


def test_config_rejects_unknown_keys_and_missing_file(tmp_path):
    import pytest as _pytest

    from diskburst.cli import resolve_args
    from diskburst.config import load_config

    bad = tmp_path / "bad.toml"
    bad.write_text("prot = 8890\n")  # typo'd key must not be silently ignored
    with _pytest.raises(SystemExit, match="unknown config key"):
        resolve_args(["--config", str(bad)])
    with _pytest.raises(SystemExit, match="not found"):
        load_config(str(tmp_path / "nope.toml"))


def test_config_default_path_missing_is_fine(tmp_path, monkeypatch):
    from diskburst.cli import resolve_args

    monkeypatch.setattr(
        "diskburst.config.DEFAULT_CONFIG_PATH", str(tmp_path / "absent.toml")
    )
    args = resolve_args([])
    assert args.port == 8890  # built-in defaults apply


def test_cache_files_are_owner_only(fixture_tree, monkeypatch, tmp_path_factory):
    from diskburst import cache

    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path_factory.mktemp("cache") / "sub"))
    scanner = scan(str(fixture_tree))
    cache.save_cache(scanner)
    path = cache.cache_path(scanner.root_paths, scanner.big_file_bytes)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_close_mid_scan_still_completes(fixture_tree):
    for _ in range(3):  # close() races worker startup; exercise it a few times
        scanner = Scanner([str(fixture_tree)], workers=4)
        scanner.start()
        scanner.close()
        assert scanner.done.wait(timeout=10), "cancelled scan never reached done"
        assert scanner.cancelled


def test_full_hash_distinguishes_content(fixture_tree):
    from diskburst.dupes import full_hash

    a = full_hash(str(fixture_tree / "a" / "big.bin"))
    (fixture_tree / "a" / "twin.bin").write_bytes(b"x" * 20_000)  # same content
    (fixture_tree / "a" / "other.bin").write_bytes(b"o" * 20_000)  # same size, diff content
    assert a == full_hash(str(fixture_tree / "a" / "twin.bin"))
    assert a != full_hash(str(fixture_tree / "a" / "other.bin"))
    assert full_hash(str(fixture_tree / "missing.bin")) is None


def test_advisor_reports_known_location(fixture_tree, monkeypatch):
    from diskburst import advisor

    trash = fixture_tree / ".Trash"
    trash.mkdir()
    (trash / "junk.bin").write_bytes(b"j" * 5_000)
    monkeypatch.setattr(advisor, "MIN_ADVISOR_BYTES", 1_000)
    monkeypatch.setattr(
        advisor, "KNOWN_LOCATIONS", [(str(trash), "Trash", "Finder → Empty Trash", "note")]
    )
    scanner = scan(str(fixture_tree))
    entries = advisor.advise(scanner)
    assert len(entries) == 1
    assert entries[0]["title"] == "Trash"
    assert entries[0]["size"] > 0


def test_rescan_dir_grafts_subtree(fixture_tree, monkeypatch, tmp_path_factory):
    from diskburst import cache
    from diskburst.server import App

    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path_factory.mktemp("cache")))
    app = App(lambda: Scanner([str(fixture_tree)], workers=4), live=False)
    app.start()
    assert app.scanner.done.wait(timeout=10)
    before = app.scanner.roots[str(fixture_tree)].total_bytes
    (fixture_tree / "b" / "new.bin").write_bytes(b"n" * 40_000)
    ok, _ = app.rescan_dir(str(fixture_tree / "b"))
    assert ok
    deadline = time.monotonic() + 10
    while app.pending_dir is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    root = app.scanner.roots[str(fixture_tree)]
    assert root.total_bytes > before
    assert "new.bin" not in root.children  # grafted under b, not at root
    assert app.scanner.find(str(fixture_tree / "b")) is not None


def test_live_watcher_applies_changes(fixture_tree, monkeypatch, tmp_path_factory):
    pytest.importorskip("watchdog")
    from diskburst import cache
    from diskburst.server import App
    from diskburst.watcher import LiveWatcher

    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path_factory.mktemp("cache")))
    # pytest tmp dirs live under /private/var/folders, which is deny-listed
    monkeypatch.setattr("diskburst.watcher.DENY_SUBSTRINGS", ())
    app = App(lambda: Scanner([str(fixture_tree)], workers=4), live=False)
    app.start()
    assert app.scanner.done.wait(timeout=10)
    watcher = LiveWatcher(app, debounce=0.2)
    watcher.start()
    try:
        before = app.scanner.roots[str(fixture_tree)].total_bytes
        (fixture_tree / "a" / "live_new.bin").write_bytes(b"L" * 60_000)
        (fixture_tree / "livedir").mkdir()
        (fixture_tree / "livedir" / "inner.bin").write_bytes(b"i" * 30_000)
        deadline = time.monotonic() + 8
        root = app.scanner.roots[str(fixture_tree)]
        while time.monotonic() < deadline:
            if root.total_bytes > before and "livedir" in root.children:
                break
            time.sleep(0.1)
        assert root.total_bytes > before
        assert "livedir" in root.children
        assert root.children["livedir"].total_bytes > 0
        assert watcher.last_change > 0
    finally:
        watcher.stop()


def test_app_rescan_swaps_scanner(fixture_tree, monkeypatch, tmp_path_factory):
    from diskburst import cache
    from diskburst.server import App

    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path_factory.mktemp("cache")))
    app = App(lambda: Scanner([str(fixture_tree)], workers=4), live=False)
    app.start()
    assert app.scanner.done.wait(timeout=10)
    first = app.scanner
    app.rescan()
    deadline = time.monotonic() + 10
    while app.pending is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert app.pending is None
    assert app.scanner is not first
    assert app.scanner.done.is_set()
    assert app.scanner.roots[str(fixture_tree)].total_bytes == first.roots[
        str(fixture_tree)
    ].total_bytes
