#!/bin/sh
set -eu

usage() {
    printf '%s\n' 'Usage: aos-connect-macos.sh [SSH_HOST]' \
        'Default SSH_HOST: AOS_HOST or cachyos (configure this SSH alias first).' \
        'AOS_REMOTE_DIR defaults to aos under the remote home; an absolute path is also supported.' \
        'Optional AOS_PROJECT and AOS_PROJECT_PORT must be paired; named projects use manual token login.' \
        'Named projects must already be started explicitly; this connector only checks their status.' \
        'Keeps a private SSH tunnel open until Ctrl-C; leaves the AOS server running.'
}

if [ "${1-}" = '--help' ]; then
    usage
    exit 0
fi
if [ "$#" -gt 1 ]; then
    usage >&2
    exit 2
fi
host=${1-${AOS_HOST-cachyos}}
remote_dir=${AOS_REMOTE_DIR-aos}
project=${AOS_PROJECT-}
port=8765
if [ "${AOS_PROJECT+x}${AOS_PROJECT_PORT+x}" != '' ]; then
    case "$project" in
        ''|[!a-z0-9]*|*[!a-z0-9-]*) printf '%s\n' 'Invalid AOS_PROJECT.' >&2; exit 2 ;;
    esac
    if [ "${#project}" -gt 48 ]; then
        printf '%s\n' 'Invalid AOS_PROJECT.' >&2; exit 2
    fi
    port=${AOS_PROJECT_PORT-}
    case "$port" in
        ''|0*|*[!0-9]*) printf '%s\n' 'AOS_PROJECT and canonical integer AOS_PROJECT_PORT are required together.' >&2; exit 2 ;;
    esac
    if [ "${#port}" -gt 5 ] || [ "$port" -lt 1024 ] || [ "$port" -gt 65535 ] || [ "$port" -eq 8765 ]; then
        printf '%s\n' 'Named project port must be 1024..65535 and not 8765.' >&2; exit 2
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        printf '%s\n' 'Named connections require an already installed python3 for strict identity verification.' >&2; exit 2
    fi
fi
origin=http://127.0.0.1:$port
case "$host" in
    ''|-*|*[!A-Za-z0-9_.@-]*) printf '%s\n' 'Invalid SSH host or alias.' >&2; exit 2 ;;
esac
case "$remote_dir" in
    aos) remote_command='cd "$HOME"/aos && ./scripts/aos-v1 start' ;;
    /*)
        case "$remote_dir" in
            *[!A-Za-z0-9_./-]*|*'/../'*|*'/./'*|*'//'*)
                printf '%s\n' 'Invalid absolute AOS_REMOTE_DIR.' >&2; exit 2 ;;
        esac
        case "$remote_dir" in
            */..|*/.) printf '%s\n' 'Invalid absolute AOS_REMOTE_DIR.' >&2; exit 2 ;;
        esac
        remote_command="cd '$remote_dir' && ./scripts/aos-v1 start"
        ;;
    *) printf '%s\n' 'AOS_REMOTE_DIR must be aos or a safe absolute path.' >&2; exit 2 ;;
esac
remote_prefix=${remote_command% start}
if [ -n "$project" ]; then
    remote_command="$remote_prefix status --project $project --project-port $port"
fi
if [ "$(uname -s)" != Darwin ]; then
    printf '%s\n' 'This connection script requires macOS; --help works on any platform.' >&2
    exit 2
fi

umask 077
private_dir=$(mktemp -d /tmp/aos-connect-macos.XXXXXXXX)
chmod 700 "$private_dir"
socket=$private_dir/control
master_started=false
cleanup() {
    if [ "$master_started" = true ]; then
        ssh -S "$socket" -O exit "$host" >/dev/null 2>&1 || :
    fi
    rm -f "$socket" "$private_dir/start.json" "$private_dir/session.json"
    rmdir "$private_dir" 2>/dev/null || :
}
trap cleanup 0
trap 'exit 130' INT
trap 'exit 143' TERM HUP

verify_named_identity() {
    python3 - "$private_dir/start.json" "$project" "$port" "${1-}" <<'PY'
import json
from pathlib import Path
import re
import sys

def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result

def invalid_constant(value):
    raise ValueError('nonfinite JSON')

def read(path):
    with Path(path).open('rb') as stream:
        content = stream.read(65537)
    if len(content) > 65536:
        raise ValueError('oversized JSON')
    value = json.loads(content, object_pairs_hook=unique, parse_constant=invalid_constant)
    if type(value) is not dict:
        raise ValueError('object required')
    return value

def scope_matches(value):
    return (type(value) is dict and set(value) == {'project', 'port'}
            and value['project'] == sys.argv[2] and type(value['port']) is int
            and value['port'] == int(sys.argv[3]))

try:
    start = read(sys.argv[1])
    if (start.get('phase') != 'running' or start.get('supervisor') != 'same_process'
            or start.get('backend') != 'same_process' or not scope_matches(start.get('manager_scope'))
            or start.get('url') != 'http://127.0.0.1:' + sys.argv[3] + '/ui/'
            or type(start.get('session')) is not str
            or re.fullmatch(r'app-[a-f0-9]{32}', start['session']) is None):
        raise ValueError('manager status identity mismatch')
    if sys.argv[4]:
        session = read(sys.argv[4])
        if (session.get('authenticated') is not False
                or session.get('local_auto_login', False) is not False
                or not scope_matches(session.get('manager_scope'))
                or session.get('manager_session') != start['session']):
            raise ValueError('public session identity mismatch')
except (OSError, ValueError, TypeError, RecursionError):
    print('Named project identity could not be verified; browser not opened.', file=sys.stderr)
    sys.exit(1)
PY
}

if ! ssh -M -S "$socket" -o ControlPersist=no -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -fNT -L "127.0.0.1:$port:127.0.0.1:$port" "$host"; then
    printf '%s\n' "SSH tunnel failed. Check SSH access and whether local port $port is already occupied." >&2
    exit 1
fi
master_started=true
if ! (ulimit -f 128; exec ssh -S "$socket" -o ControlMaster=no -o BatchMode=yes "$host" "$remote_command") >"$private_dir/start.json"; then
    printf '%s\n' 'AOS manager command failed. Inspect the server locally; no restart or orphan cleanup was attempted.' >&2
    exit 1
fi
if [ -n "$project" ]; then
    printf '%s\n' 'Checking the already-started named project; no start or authorization is attempted.'
    verify_named_identity
fi

deadline=$(( $(date +%s) + 30 ))
ready=false
while [ "$(date +%s)" -lt "$deadline" ]; do
    if [ -n "$project" ]; then
        if (ulimit -f 128; exec curl --silent --show-error --fail --noproxy '*' --max-time 1 \
            --max-filesize 65536 "$origin/api/session") >"$private_dir/session.json" 2>/dev/null; then
            verify_named_identity "$private_dir/session.json"
            ready=true
            break
        fi
        sleep 1
        continue
    fi
    if session=$(curl --silent --show-error --fail --noproxy '*' --max-time 1 \
        http://127.0.0.1:8765/api/session 2>/dev/null); then
        if printf '%s' "$session" | grep -Eq '"local_auto_login"[[:space:]]*:[[:space:]]*true[[:space:]]*[,}]'; then
            ready=true
            break
        fi
        printf '%s\n' 'Local login is unavailable. Update the server and perform a safe idle restart locally.' >&2
        exit 1
    fi
    sleep 1
done
if [ "$ready" != true ]; then
    printf '%s\n' 'AOS did not become ready within 30 seconds; the browser was not opened.' >&2
    exit 1
fi
open "$origin/ui/"
if [ -n "$project" ]; then
    printf '%s\n' 'Named project verified. Automatic login is disabled; use manual token login.' \
        "In a separate SSH session to $host, run: $remote_prefix token --project $project --project-port $port" \
        'Keep the token private. This connector never reads or copies it.'
fi
printf '%s\n' 'AOS opened. Keep this terminal open; Ctrl-C closes only this SSH tunnel.'
while ssh -S "$socket" -O check "$host" >/dev/null 2>&1; do
    sleep 1
done
printf '%s\n' 'SSH tunnel disconnected. The AOS server remains running.'
