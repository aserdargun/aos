import os
import time


def require_empty_cgroup(control_group, deadline, *, collected, read, require, clock=time.monotonic):
    require(control_group.startswith('/') and all(part not in {'', '.', '..'}
            for part in control_group.split('/')[1:]), 'Physical cgroup path is not exact')
    mounts = [line.split() for line in read('/proc/self/mountinfo', max_bytes=262144).splitlines()]
    matching = [row for row in mounts if len(row) > 6 and row[4] == '/sys/fs/cgroup']
    require(len(matching) == 1 and '-' in matching[0]
             and matching[0][matching[0].index('-') + 1] == 'cgroup2',
             'Physical cgroup2 mount is unavailable or ambiguous')
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    descriptor = os.open('/sys/fs/cgroup', flags)
    try:
        read('cgroup.procs', directory=descriptor)
        read('cgroup.controllers', directory=descriptor)
        for part in control_group.split('/')[1:]:
            try:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                require(collected, 'Physical retained cgroup unexpectedly disappeared')
                return
            os.close(descriptor)
            descriptor = next_descriptor
        visited = 0

        def inspect(directory):
            nonlocal visited
            visited += 1
            require(visited <= 128 and clock() < deadline,
                     'Physical cgroup observation exceeds its bound')
            require(not read('cgroup.procs', directory=directory).strip(),
                     'Physical child cgroup contains processes')
            rows = [line.split() for line in read('cgroup.events', directory=directory).splitlines()]
            require(all(len(row) == 2 for row in rows), 'Physical cgroup events are malformed')
            events = dict(rows)
            require(len(rows) == len(events) and events.get('populated') == '0',
                     'Physical child cgroup is recursively populated or ambiguous')
            with os.scandir(directory) as entries:
                for position, entry in enumerate(entries):
                    require(position < 256 and clock() < deadline,
                             'Physical cgroup entries exceed their bound')
                    require(not entry.is_symlink(), 'Physical cgroup contains a symlink')
                    if entry.is_dir(follow_symlinks=False):
                        nested = os.open(entry.name, flags, dir_fd=directory)
                        try:
                            inspect(nested)
                        finally:
                            os.close(nested)
        inspect(descriptor)
    finally:
        os.close(descriptor)
