"""Advisor: recognize known reclaimable locations and show their official clean command.

Strictly informational — commands are displayed for the user to copy and run
themselves; nothing here executes anything.
"""

from __future__ import annotations

import os

from .scanner import Scanner

MIN_ADVISOR_BYTES = 50 * 1000 * 1000

# (relative-to-home | absolute path, title, command, note)
KNOWN_LOCATIONS: list[tuple[str, str, str, str]] = [
    ("~/.Trash", "Trash", "Finder → Empty Trash", "Everything here is already marked for deletion."),
    ("~/.cache/uv", "uv cache", "uv cache prune", "Removes unused wheels; kept packages re-download on demand."),
    ("~/.cache/huggingface", "Hugging Face cache", "hf cache delete", "Interactive picker for cached models/datasets (older CLIs: huggingface-cli delete-cache)."),
    ("~/.cache/pip", "pip cache", "pip cache purge", "Safe; wheels re-download on next install."),
    ("~/.cache/torch", "torch hub cache", "rm -rf ~/.cache/torch", "Checkpoints/models re-download on next use."),
    ("~/.npm", "npm cache", "npm cache clean --force", "Safe; verified re-download on next install."),
    ("~/.ollama/models", "Ollama models", "ollama list  # then: ollama rm <model>", "Remove models you no longer run locally."),
    ("~/Library/Caches", "App caches (~/Library/Caches)", "— (apps rebuild their own caches)", "Broadly safe but indiscriminate; prefer per-app cleanup for the big ones inside."),
    ("~/Library/Caches/Homebrew", "Homebrew downloads", "brew cleanup --prune=all", "Also trims old versions in /opt/homebrew/Cellar."),
    ("~/Library/Developer/Xcode/DerivedData", "Xcode DerivedData", "rm -rf ~/Library/Developer/Xcode/DerivedData", "Pure build products; Xcode rebuilds them."),
    ("~/Library/Developer/Xcode/iOS DeviceSupport", "Xcode iOS DeviceSupport", "rm -rf ~/'Library/Developer/Xcode/iOS DeviceSupport'", "Symbols re-extract from a connected device when needed."),
    ("~/Library/Developer/CoreSimulator", "Xcode simulators", "xcrun simctl delete unavailable", "Deletes simulators for SDKs you no longer have."),
    ("~/Library/Containers/com.docker.docker", "Docker data", "docker system prune -a", "Removes stopped containers, unused images and build cache."),
    ("~/Library/Application Support/Claude/vm_bundles", "Claude Code VM images", "— (re-downloaded on next VM use)", "Safe to remove if you don't use Claude Code cloud VMs."),
    ("~/go/pkg/mod", "Go module cache", "go clean -modcache", "Safe; modules re-download."),
    ("~/.cargo/registry", "Cargo registry cache", "cargo cache --autoclean  # (cargo install cargo-cache)", "Or rm the registry; crates re-download."),
    ("~/miniconda3/pkgs", "Conda package cache", "conda clean --all", "Safe; removes unused package tarballs."),
    ("~/anaconda3/pkgs", "Conda package cache", "conda clean --all", "Safe; removes unused package tarballs."),
]


def advise(scanner: Scanner) -> list[dict]:
    """Known reclaimable locations present in the scanned tree, largest first."""
    entries = []
    for raw_path, title, command, note in KNOWN_LOCATIONS:
        path = os.path.expanduser(raw_path)
        found = scanner.find(path)
        if found is None:
            continue
        node, _ = found
        if node.total_bytes < MIN_ADVISOR_BYTES:
            continue
        entries.append(
            {
                "title": title,
                "path": path,
                "size": node.total_bytes,
                "files": node.total_files,
                "command": command.replace("~", os.path.expanduser("~")),
                "note": note,
            }
        )
    node_modules = _aggregate_named_dirs(scanner, "node_modules")
    if node_modules["size"] >= MIN_ADVISOR_BYTES:
        entries.append(
            {
                "title": f"node_modules ({node_modules['count']} projects)",
                "path": "",
                "size": node_modules["size"],
                "files": 0,
                "command": "npx npkill",
                "note": "Interactive picker; removed folders restore with npm/yarn/pnpm install.",
            }
        )
    entries.sort(key=lambda e: e["size"], reverse=True)
    return entries


def _aggregate_named_dirs(scanner: Scanner, dirname: str) -> dict:
    """Total size of all dirs with `dirname` (not nested inside another one)."""
    total = 0
    count = 0
    for root_path, root in scanner.roots.items():
        stack = [root]
        while stack:
            node = stack.pop()
            for name, child in list(node.children.items()):
                if name == dirname:
                    total += child.total_bytes
                    count += 1
                else:
                    stack.append(child)
    return {"size": total, "count": count}
