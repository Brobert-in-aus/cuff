"""Saving state files so a crash, a power cut or a full disk mid-write never leaves half a file
behind. Both the running Cuff and cuff.remote (Cuff for PC's settings) write these, and their
loaders fall back to the defaults on a file they can't parse, so a truncated file used to lose
every setting in it.

The new contents go to a temporary file in the same folder, are flushed to disk, then replace
the old file in one step (os.replace): a reader sees the old file or the new one, never a mix.
"""
import json
import os
import stat
import tempfile


def atomic_write_json(path, data, **dump_args):
    """Writes data to path as JSON (dump_args go to json.dump, e.g. indent=1), atomically, making
    the folder if needed. Raises OSError (or TypeError / ValueError for data JSON can't hold); then
    the old file is untouched and the temporary one is gone."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, **dump_args)
            f.flush()
            os.fsync(f.fileno())
        try:  # mkstemp makes it private; keep the old file's permissions if there was one
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:  # and the rename itself, so it survives a power cut
        dir_fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
