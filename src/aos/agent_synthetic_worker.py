import hashlib
import json
import os
import resource
import signal
import stat
import sys
import time


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def main():
    if len(sys.argv) != 3:
        raise ValueError('An inherited private workspace descriptor and exact request hash are required')
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_AS, (268435456, 268435456))
    resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    inherited = int(sys.argv[1])
    if inherited < 3:
        raise ValueError('Invalid synthetic workspace descriptor')
    directory = os.dup(inherited)
    os.close(inherited)
    try:
        metadata = os.fstat(directory)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700):
            raise ValueError('Synthetic workspace must be private and owned')
        descriptor = os.open('request.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                    or metadata.st_size > 65536):
                raise ValueError('Invalid private synthetic request')
            raw = os.read(descriptor, 65537)
        finally:
            os.close(descriptor)
        request = json.loads(raw)
        if hashlib.sha256(canonical(request).encode()).hexdigest() != sys.argv[2]:
            raise ValueError('Synthetic request hash differs')
        wall_seconds = request['budget']['wall_seconds']
        if type(wall_seconds) is not int or not 1 <= wall_seconds <= 30:
            raise ValueError('Invalid synthetic wall-clock budget')
        signal.setitimer(signal.ITIMER_REAL, wall_seconds)
        payload = request['payload']
        if (request['operation'] != 'synthetic.write.v1' or set(payload) != {'text', 'delay_ms'}
                or type(payload['text']) is not str or len(payload['text'].encode()) > 4096
                or type(payload['delay_ms']) is not int or not 0 <= payload['delay_ms'] <= 10000):
            raise ValueError('Synthetic worker accepts only its bounded fixed operation')
        time.sleep(payload['delay_ms'] / 1000)
        result = {'schema_version': 'aos.synthetic-result.v1', 'synthetic': True,
                  'job_id': request['job_id'], 'request_sha256': sys.argv[2],
                  'text': payload['text'], 'text_sha256': hashlib.sha256(payload['text'].encode()).hexdigest()}
        descriptor = os.open('result.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, 'wb', closefd=False) as output:
                output.write(canonical(result).encode())
                output.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory)
    finally:
        os.close(directory)


if __name__ == '__main__':
    main()
