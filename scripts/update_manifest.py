import hashlib
import os
from pathlib import Path
import secrets
import stat

try:
    from .package_handoff import _directory, _identity, collect_source_paths, read_source_member
except ImportError:
    from package_handoff import _directory, _identity, collect_source_paths, read_source_member


ROOT = Path(__file__).resolve().parents[1]


def main():
    files = collect_source_paths(ROOT)
    lines = [f"{hashlib.sha256(read_source_member(ROOT, name).data).hexdigest()}  {name}\n"
             for name in files]
    with _directory(ROOT) as parent:
        try:
            previous = os.stat('MANIFEST.sha256', dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            previous = None
        if previous is not None and (not stat.S_ISREG(previous.st_mode) or previous.st_nlink != 1):
            raise ValueError('manifest must be a single-link regular file')
        temporary = '.manifest-' + secrets.token_hex(16) + '.tmp'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o644, dir_fd=parent)
        try:
            with os.fdopen(descriptor, 'w') as output:
                output.write(''.join(lines))
                output.flush()
                os.fsync(output.fileno())
            try:
                current = os.stat('MANIFEST.sha256', dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if ((previous is None) != (current is None)
                    or previous is not None and _identity(previous) != _identity(current)):
                raise ValueError('manifest changed before publication')
            os.replace(temporary, 'MANIFEST.sha256', src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    print(f"Updated source manifest: {len(files)} files; local artifacts excluded.")


if __name__ == "__main__":
    main()
