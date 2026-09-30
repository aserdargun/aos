#!/bin/sh
set -eu

usage() {
    printf '%s\n' 'Usage: aos-connect-macos.sh [SSH_HOST]' \
        'Default SSH_HOST: AOS_HOST or cachyos (configure this SSH alias first).' \
        'AOS_REMOTE_DIR defaults to aos under the remote home; an absolute path is also supported.' \
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
    rm -f "$socket" "$private_dir/start.json"
    rmdir "$private_dir" 2>/dev/null || :
}
trap cleanup 0
trap 'exit 130' INT
trap 'exit 143' TERM HUP

if ! ssh -M -S "$socket" -o ControlPersist=no -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -fNT -L 127.0.0.1:8765:127.0.0.1:8765 "$host"; then
    printf '%s\n' 'SSH tunnel failed. Check SSH access and whether local port 8765 is already occupied.' >&2
    exit 1
fi
master_started=true
if ! ssh -S "$socket" -o ControlMaster=no -o BatchMode=yes "$host" "$remote_command" >"$private_dir/start.json"; then
    printf '%s\n' 'AOS start failed. Inspect the server locally; no restart or orphan cleanup was attempted.' >&2
    exit 1
fi

deadline=$(( $(date +%s) + 30 ))
ready=false
while [ "$(date +%s)" -lt "$deadline" ]; do
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
open http://127.0.0.1:8765/ui/
printf '%s\n' 'AOS opened. Keep this terminal open; Ctrl-C closes only this SSH tunnel.'
while ssh -S "$socket" -O check "$host" >/dev/null 2>&1; do
    sleep 1
done
printf '%s\n' 'SSH tunnel disconnected. The AOS server remains running.'
