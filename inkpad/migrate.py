"""Moving the whole library to another folder.

Everything in the old folder, hidden bookkeeping included, ends up in the new one
and the old folder is removed if that leaves it empty. On the same disk this is a
rename per item and takes no time. Across disks every file is copied and its size
checked, and nothing old is deleted until all of it has arrived. If anything goes
wrong part-way, what was moved is put back and the old library is left as it was.
"""
import os
import shutil

from .i18n import tr


class MigrateError(Exception):
    pass


def _device(path):
    while not os.path.exists(path):
        path = os.path.dirname(path)
    return os.stat(path).st_dev, path


def _files(root):
    """Every file under root as (relative path, size), plus every directory."""
    files, dirs = [], []
    for base, names_d, names_f in os.walk(root):
        rel = os.path.relpath(base, root)
        for d in names_d:
            dirs.append(os.path.normpath(os.path.join(rel, d)))
        for f in names_f:
            path = os.path.join(base, f)
            if not os.path.islink(path):
                files.append((os.path.normpath(os.path.join(rel, f)), os.path.getsize(path)))
    return files, dirs


def check(old, new):
    """Raise MigrateError when the library cannot be moved from old to new."""
    old, new = os.path.abspath(old), os.path.abspath(new)
    if new == old:
        raise MigrateError(tr('这已经是当前的文稿库位置'))
    if new.startswith(old + os.sep) or old.startswith(new + os.sep):
        raise MigrateError(tr('新位置不能在当前文稿库里面，也不能包含它'))
    if os.path.exists(new):
        if not os.path.isdir(new):
            raise MigrateError(tr('新位置不是文件夹'))
        if os.listdir(new):
            raise MigrateError(tr('新位置必须是空文件夹'))
    parent = _device(new)[1]
    if not os.access(parent, os.W_OK):
        raise MigrateError(tr('没有写入新位置的权限'))


def move_library(old, new, progress=None):
    """Move the library; progress(fraction, text) is called as it goes. Raises MigrateError."""
    old, new = os.path.abspath(old), os.path.abspath(new)
    check(old, new)
    report = progress or (lambda fraction, text: None)
    made_new = not os.path.exists(new)
    same_disk = _device(new)[0] == os.stat(old).st_dev
    try:
        os.makedirs(new, exist_ok=True)
    except OSError as exc:
        raise MigrateError(str(exc))

    if same_disk:
        names = sorted(os.listdir(old))
        moved = []
        try:
            for n, name in enumerate(names):
                report(n / max(1, len(names)), name)
                os.rename(os.path.join(old, name), os.path.join(new, name))
                moved.append(name)
        except OSError as exc:
            for name in reversed(moved):   # put back what was moved
                os.rename(os.path.join(new, name), os.path.join(old, name))
            if made_new:
                shutil.rmtree(new, ignore_errors=True)
            raise MigrateError(str(exc))
    else:
        files, dirs = _files(old)
        total = sum(size for _, size in files)
        free = shutil.disk_usage(new).free
        if free < total + (16 << 20):
            if made_new:
                shutil.rmtree(new, ignore_errors=True)
            raise MigrateError(tr('新位置的可用空间不足：需要 %s，只有 %s') % (_size(total), _size(free)))
        done = 0
        try:
            for rel in dirs:
                os.makedirs(os.path.join(new, rel), exist_ok=True)
            for rel, size in files:
                report(done / max(1, total), rel)
                target = os.path.join(new, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(os.path.join(old, rel), target)
                if os.path.getsize(target) != size:
                    raise OSError(tr('复制后大小不一致：%s') % rel)
                done += size
        except OSError as exc:
            # Nothing old has been touched yet: only the copies have to go.
            if made_new:
                shutil.rmtree(new, ignore_errors=True)
            else:
                for name in os.listdir(new):
                    path = os.path.join(new, name)
                    shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
            raise MigrateError(str(exc))
        # Everything has arrived and been checked; now the old copies can go.
        for rel, _ in files:
            try:
                os.remove(os.path.join(old, rel))
            except OSError:
                pass
        for rel in sorted(dirs, key=len, reverse=True):
            try:
                os.rmdir(os.path.join(old, rel))
            except OSError:
                pass
    report(1.0, '')
    try:
        os.rmdir(old)   # only succeeds when nothing is left in it
    except OSError:
        pass
    return new


def _size(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return ('%.1f %s' % (n, unit)) if unit != 'B' else '%d B' % n
        n /= 1024
