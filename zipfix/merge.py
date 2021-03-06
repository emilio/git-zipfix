"""
This module contains a basic implementation of an efficient, in-memory 3-way
git tree merge. This is used rather than traditional git mechanisms to avoid
needing to use the index file format, which can be slow to initialize for
large repositories.

The INDEX file for my local mozilla-central checkout, for reference, is 35MB.
While this isn't huge, it takes a perceptable amount of time to read the tree
files and generate. This algorithm, on the other hand, avoids looking at
unmodified trees and blobs when possible.
"""

from .odb import Oid, GitObj, Tree, Blob, Commit, Entry, Mode
from typing import Sequence, Optional, Type, Tuple, TypeVar
from tempfile import TemporaryDirectory
from pathlib import Path
import subprocess


T = TypeVar('T')


class MergeConflict(Exception):
    pass


def rebase(commit: Commit, parent: Commit) -> Commit:
    if commit.parent() == parent:
        return commit  # No need to do anything

    tree = merge_trees(Path('/'), ('new parent', 'old parent', 'incoming'),
                       parent.tree(), commit.parent().tree(), commit.tree())
    # NOTE: This omits commit.committer to pull it from the environment. This
    # means that created commits may vary between invocations, but due to
    # caching, should be consistent within a single process.
    return Commit.create(tree.oid, [parent.oid], commit.message, commit.author)


def conflict_prompt(path: Path, descr: str,
                    labels: Tuple[str, str, str],
                    current: T, current_descr: str,
                    other: T, other_descr: str) -> T:
    print(f"{descr} conflict for '{path}'")
    print(f"  (1) {labels[0]}: {current_descr}")
    print(f"  (2) {labels[1]}: {other_descr}")
    ch = input("Resolution or (A)bort? ")
    if ch == '1':
        return current
    elif ch == '2':
        return other
    raise MergeConflict('aborted')


def merge_trees(path: Path,
                labels: Tuple[str, str, str],
                current: Tree,
                base: Tree,
                other: Tree) -> Tree:
    # Merge every named entry which is mentioned in any tree.
    names = set(current.entries.keys()).union(base.entries.keys(),
                                              other.entries.keys())
    entries = {}
    for name in names:
        merged = merge_entries(path / name.decode(errors='replace'),
                               labels,
                               current.entries.get(name),
                               base.entries.get(name),
                               other.entries.get(name))
        if merged is not None:
            entries[name] = merged
    return Tree.create(entries)


def merge_entries(path: Path,
                  labels: Tuple[str, str, str],
                  current: Optional[Entry],
                  base: Optional[Entry],
                  other: Optional[Entry]) -> Optional[Entry]:
    if base == current:
        return other  # no change from base -> current
    elif base == other:
        return current  # no change from base -> other
    elif current == other:
        return current  # base -> current & base -> other are identical

    # If one of the branches deleted the entry, and the other modified it,
    # report a merge conflict.
    if current is None:
        return conflict_prompt(path, "Deletion", labels,
                               current, "deleted",
                               other, "modified")
    if other is None:
        return conflict_prompt(path, "Deletion", labels,
                               current, "modified",
                               other, "deleted")

    # Determine which mode we're working with here.
    if current.mode == other.mode:
        mode = current.mode  # current & other agree
    elif current.mode.is_file() and other.mode.is_file():
        # File types support both Mode.EXEC and Mode.REGULAR, try to pick one.
        if base and base.mode == current.mode:
            mode = other.mode
        elif base and base.mode == other.mode:
            mode = current.mode
        else:
            mode = Mode.EXEC  # XXX: gross
    else:
        return conflict_prompt(path, "Entry type", labels,
                               current, str(current.mode),
                               other, str(other.mode))

    # Time to merge the actual entries!
    if mode.is_file():
        baseblob = None
        if base and base.mode.is_file():
            baseblob = base.blob()
        return Entry(mode, merge_blobs(path, labels,
                                       current.blob(),
                                       baseblob,
                                       other.blob()).oid)
    elif mode == Mode.DIR:
        basetree = Tree.empty()
        if base and base.mode == Mode.DIR:
            basetree = base.tree()
        return Entry(mode, merge_trees(path, labels, current.tree(),
                                       basetree, other.tree()).oid)
    elif mode == Mode.SYMLINK:
        return conflict_prompt(path, "Symlink", labels,
                               current, current.symlink().decode(),
                               other, other.symlink().decode())
    elif mode == Mode.GITLINK:
        return conflict_prompt(path, "Submodule", labels,
                               current, str(current.oid),
                               other, str(other.oid))
    else:
        raise ValueError("unknown mode")


def merge_blobs(path: Path,
                labels: Tuple[str, str, str],
                current: Blob,
                base: Optional[Blob],
                other: Blob) -> Blob:
    with TemporaryDirectory() as tmpdir_name:
        tmpdir = Path(tmpdir_name)
        with open(tmpdir / 'current', 'wb') as f:
            f.write(current.body)
        with open(tmpdir / 'base', 'wb') as f:
            if base is not None:
                f.write(base.body)
        with open(tmpdir / 'other', 'wb') as f:
            f.write(other.body)

        current_lbl = f"{path} ({labels[0]})"
        base_lbl = f"{path} ({labels[1]})"
        other_lbl = f"{path} ({labels[2]})"

        # Try running git merge-file to automatically resolve conflicts.
        process = subprocess.run([
            'git', 'merge-file', '-q', '-p',
            '-L', current_lbl, '-L', base_lbl, '-L', other_lbl,
            tmpdir / 'current', tmpdir / 'base', tmpdir / 'other'
        ], stdout=subprocess.PIPE)

        # The return code of git merge-file is '0' if there were no conflicts,
        # negative if there was an error, and the positive number of conficts
        # if there were conflicts.
        if process.returncode == 0:
            return Blob(process.stdout)
        elif process.returncode < 0:
            raise MergeConflict("git merge-file errored")

        # Our merge failed with conflicts. Instead try calling an interactive
        # merge handler. Display a prompt to confirm fixes have been made.
        # XXX(nika): Support multiple mergetools?
        subprocess.run([
            'kdiff3', '--merge', '--auto', '-o', tmpdir / 'merged',
            # kdiff3 takes base as the first file.
            '--L1', base_lbl, '--L2', current_lbl, '--L3', other_lbl,
            tmpdir / 'base', tmpdir / 'current', tmpdir / 'other',
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if input('Have conflicts resolved successfully? (y/N) ').lower() != 'y':
            raise MergeConflict("merging failed")

        with open(tmpdir / 'merged', 'rb') as f:
            return Blob(f.read())
