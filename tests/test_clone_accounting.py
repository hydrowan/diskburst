"""Clone-aware sizes: synthetic ledger trees everywhere, real `cp -c` clones on APFS."""

import json
import os
import subprocess
import sys

import pytest

from diskburst import bulk_listing
from diskburst.clone_aware_report import collect_rows, main
from diskburst.clones import CloneLedger, lowest_common_ancestor, preorder_index
from diskburst.scanner import BLOCK_BYTES, DirNode, Scanner, compute_totals

FILE_BYTES = 256 * 1024
EDIT_OFFSET = 1000


def tree(*names: str) -> tuple[DirNode, dict[str, DirNode]]:
    """Build root/{names...} where a name like 'a/b' nests b under a."""
    root, nodes = DirNode(name="root"), {}
    for name in names:
        parent = nodes[name.rsplit("/", 1)[0]] if "/" in name else root
        nodes[name] = parent.children[name.rsplit("/", 1)[-1]] = DirNode(name=name)
    return root, nodes


def test_lowest_common_ancestor_and_preorder():
    root, n = tree("a", "a/x", "a/y", "b")
    order, parents, depths, _ = preorder_index([root])
    ax, ay, b = order[id(n["a/x"])], order[id(n["a/y"])], order[id(n["b"])]
    assert lowest_common_ancestor(ax, ay, parents, depths) == order[id(n["a"])]
    assert lowest_common_ancestor(ax, b, parents, depths) == order[id(root)]
    assert order[id(n["a"])] < ax and order[id(n["a"])] < ay  # preorder


def test_ledger_complete_group_credits_lca_once():
    root, n = tree("a", "a/x", "a/y", "b")
    ledger = CloneLedger()
    ledger.record(n["a/x"], [(7, 100, 0, 2)], [])
    ledger.record(n["a/y"], [(7, 100, 0, 2)], [])
    ledger.apply([root])
    compute_totals(root)
    assert (n["a/x"].unique_bytes, n["a/x"].freed_bytes) == (100, 0)
    assert (n["a"].unique_bytes, n["a"].freed_bytes) == (100, 100)
    assert (root.unique_bytes, root.freed_bytes) == (100, 100)
    assert n["b"].unique_bytes == 0


def test_ledger_incomplete_group_frees_only_private():
    root, n = tree("a", "b")
    ledger = CloneLedger()
    ledger.record(n["a"], [(7, 100, 5, 3)], [])
    ledger.record(n["b"], [(7, 100, 0, 3)], [])
    n["a"].own_freed = 5  # the scanner adds private bytes as it lists
    ledger.apply([root])
    compute_totals(root)
    assert (root.unique_bytes, root.freed_bytes) == (100, 5)


def test_ledger_group_across_roots_has_no_common_ancestor():
    left, right = DirNode(name="left"), DirNode(name="right")
    ledger = CloneLedger()
    ledger.record(left, [(7, 100, 0, 2)], [])
    ledger.record(right, [(7, 100, 0, 2)], [])
    ledger.apply([left, right])
    assert (left.own_unique, left.own_freed, right.own_unique) == (100, 0, 100)


def apfs_clone(src, dst) -> None:
    if sys.platform != "darwin" or not bulk_listing.AVAILABLE:
        pytest.skip("APFS clones need macOS")
    if subprocess.run(["cp", "-c", str(src), str(dst)], capture_output=True).returncode:
        pytest.skip("tmp dir is not on a clone-capable (APFS) volume")


def alloc(path) -> int:
    return os.lstat(path).st_blocks * BLOCK_BYTES


def private_bytes(directory) -> dict[str, int]:
    return {e[1]: e[7] for e in bulk_listing.list_directory(str(directory))}


@pytest.fixture
def clone_tree(tmp_path):
    """inside/{orig,clone}; left/split + right/split_clone; hard/{file,link}; mod/{base,edited}."""
    for d in ("inside", "left", "right", "hard", "mod"):
        (tmp_path / d).mkdir()
    for path in ("inside/orig.bin", "left/split.bin", "hard/file.bin", "mod/base.bin"):
        (tmp_path / path).write_bytes(os.urandom(FILE_BYTES))
    apfs_clone(tmp_path / "inside/orig.bin", tmp_path / "inside/clone.bin")
    apfs_clone(tmp_path / "left/split.bin", tmp_path / "right/split_clone.bin")
    apfs_clone(tmp_path / "mod/base.bin", tmp_path / "mod/edited.bin")
    with open(tmp_path / "mod/edited.bin", "r+b") as f:
        f.seek(EDIT_OFFSET)
        f.write(b"edited")
    os.link(tmp_path / "hard/file.bin", tmp_path / "hard/link.bin")
    return tmp_path


def scan_clone_aware(root) -> Scanner:
    scanner = Scanner([str(root)], workers=4, clone_aware=True)
    scanner.start()
    assert scanner.done.wait(timeout=10)
    return scanner


def sizes(node: DirNode) -> tuple[int, int, int]:
    return node.total_bytes, node.unique_bytes, node.freed_bytes


def test_clone_pair_inside_one_tree(clone_tree):
    node = scan_clone_aware(clone_tree).roots[str(clone_tree)].children["inside"]
    size = alloc(clone_tree / "inside/orig.bin")
    assert sizes(node) == (2 * size, size, size)


def test_clone_pair_split_across_siblings(clone_tree):
    root = scan_clone_aware(clone_tree).roots[str(clone_tree)]
    size = alloc(clone_tree / "left/split.bin")
    assert sizes(root.children["left"]) == (size, size, 0)
    assert sizes(root.children["right"]) == (size, size, 0)
    assert root.freed_bytes - sum(c.freed_bytes for c in root.children.values()) == size


def test_hard_links_in_one_dir_unique_once(clone_tree):
    node = scan_clone_aware(clone_tree).roots[str(clone_tree)].children["hard"]
    size = alloc(clone_tree / "hard/file.bin")
    assert node.total_files == 2
    assert sizes(node) == (2 * size, size, size)


def file_names_under(path) -> int:
    return sum(len(files) for _, _, files in os.walk(path))


def test_hard_link_split_across_siblings_counts_both_names(tmp_path):
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
    (tmp_path / "a/x.bin").write_bytes(os.urandom(FILE_BYTES))
    os.link(tmp_path / "a/x.bin", tmp_path / "b/x_link.bin")
    (tmp_path / "b/own.bin").write_bytes(os.urandom(FILE_BYTES // 4))
    root = scan_clone_aware(tmp_path).roots[str(tmp_path)]
    size, own = alloc(tmp_path / "a/x.bin"), alloc(tmp_path / "b/own.bin")
    for d in ("a", "b"):
        assert root.children[d].total_files == file_names_under(tmp_path / d)
    assert sizes(root.children["a"]) == (size, size, 0)
    assert sizes(root.children["b"]) == (size + own, size + own, own)
    assert sizes(root) == (2 * size + own, size + own, size + own)


def test_hard_linked_clone_member_placed_at_common_ancestor(tmp_path):
    for d in ("c", "d"):
        (tmp_path / d).mkdir()
    (tmp_path / "c/orig.bin").write_bytes(os.urandom(FILE_BYTES))
    apfs_clone(tmp_path / "c/orig.bin", tmp_path / "c/clone.bin")
    os.link(tmp_path / "c/orig.bin", tmp_path / "d/orig_link.bin")
    root = scan_clone_aware(tmp_path).roots[str(tmp_path)]
    size = alloc(tmp_path / "c/orig.bin")
    assert sizes(root.children["c"]) == (2 * size, size, 0)
    assert sizes(root.children["d"]) == (size, size, 0)
    assert sizes(root) == (3 * size, size, size)


def test_failed_bulk_listing_is_counted_and_falls_back(clone_tree, monkeypatch):
    real = bulk_listing.list_directory
    failing = str(clone_tree / "left")

    def flaky(path):
        if path == failing:
            raise OSError(4, "Interrupted system call")
        return real(path)

    monkeypatch.setattr(bulk_listing, "list_directory", flaky)
    scanner = scan_clone_aware(clone_tree)
    assert scanner.stats.listing_failures == 1
    assert scanner.roots[str(clone_tree)].children["left"].total_files == 1


def test_modified_clone_frees_only_private_bytes(clone_tree):
    node = scan_clone_aware(clone_tree).roots[str(clone_tree)].children["mod"]
    private = private_bytes(clone_tree / "mod")
    apparent = alloc(clone_tree / "mod/base.bin") + alloc(clone_tree / "mod/edited.bin")
    assert private["edited.bin"] < alloc(clone_tree / "mod/edited.bin")
    assert sizes(node) == (apparent, apparent, private["base.bin"] + private["edited.bin"])


def test_plain_scan_reports_apparent_for_all_three(clone_tree):
    scanner = Scanner([str(clone_tree)], workers=4)
    scanner.start()
    assert scanner.done.wait(timeout=10)
    root = scanner.roots[str(clone_tree)]
    assert root.unique_bytes == root.freed_bytes == root.total_bytes


def test_clone_aware_apparent_is_plain_scan_plus_extra_link_names(clone_tree):
    plain = Scanner([str(clone_tree)], workers=4)
    plain.start()
    assert plain.done.wait(timeout=10)
    aware = scan_clone_aware(clone_tree).roots[str(clone_tree)]
    link = alloc(clone_tree / "hard/link.bin")
    assert aware.total_bytes == plain.roots[str(clone_tree)].total_bytes + link
    assert aware.total_files == plain.roots[str(clone_tree)].total_files + 1
    assert aware.total_files == file_names_under(clone_tree)


def test_cache_round_trips_clone_sizes(clone_tree, monkeypatch, tmp_path_factory):
    from diskburst import cache

    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path_factory.mktemp("cache")))
    scanner = scan_clone_aware(clone_tree)
    cache.save_cache(scanner)
    loaded = cache.load_cache([str(clone_tree)], scanner.big_file_bytes, workers=4)
    assert sizes(loaded.roots[str(clone_tree)]) == sizes(scanner.roots[str(clone_tree)])


def test_report_rows_sorted_by_freed_and_depth_limited(clone_tree):
    rows = collect_rows(scan_clone_aware(clone_tree), depth=1)
    assert len(rows) == 6  # root + five children
    freed = [r["freed_if_deleted_bytes"] for r in rows]
    assert freed == sorted(freed, reverse=True)


def test_report_json_output(clone_tree, capsys):
    main([str(clone_tree), "--depth", "1", "--limit", "3", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert len(report["rows"]) == 3 and report["clone_aware"] and report["files"] == 8
    assert report["rows"][0]["path"] == str(clone_tree)
