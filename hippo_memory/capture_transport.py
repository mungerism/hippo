"""Optional same-user Unix transport to the existing Worker; no queue policy here."""

import json
import os
import socket
import stat

MAX_REQUEST = 8 * 1024 * 1024


def socket_path(spool_dir, version):
    base = os.fspath(spool_dir)
    return os.path.join(base, 'v2', 'capture.sock') if version == 2 else os.path.join(base, 'capture.sock')


def receive(stream, limit):
    header = bytearray()
    while len(header) < 4:
        part = stream.recv(4 - len(header))
        if not part:
            raise ValueError('Incomplete frame')
        header.extend(part)
    size = int.from_bytes(header, 'big')
    if size > limit:
        raise ValueError('Frame too large')
    body = bytearray()
    while len(body) < size:
        part = stream.recv(min(65536, size - len(body)))
        if not part:
            raise ValueError('Incomplete frame')
        body.extend(part)
    return json.loads(body)


def send(stream, value, limit):
    body = json.dumps(value).encode()
    if len(body) > limit:
        raise ValueError('Frame too large')
    stream.sendall(len(body).to_bytes(4, 'big') + body)


def capture_via_worker(*, spool_dir, version, host, cwd, raw_input):
    """Return host response only after durable publication, else request local fallback."""
    try:
        # A resident process cannot interpret paths relative to the Hook process.
        # Keep those legacy inputs on their original local path.
        data = json.loads(raw_input)
        if not isinstance(data, dict) or (cwd and not os.path.isabs(cwd)):
            return None
        for key in ('transcriptPath', 'transcript_path', 'transcript_file', 'cwd', 'workspace_root', 'project_dir'):
            value = data.get(key)
            if value and (not isinstance(value, str) or not os.path.isabs(value)):
                return None
        roots = data.get('workspacePaths', [])
        if not isinstance(roots, list) or any(not isinstance(p, str) or not os.path.isabs(p) for p in roots):
            return None
        if host == 'zcode' and not cwd:
            cwd = os.getcwd()
        path = socket_path(spool_dir, version)
        info = os.lstat(path)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(0.025)
            stream.connect(path)
            send(stream, {'host': host, 'cwd': cwd, 'raw_input': raw_input, 'version': version}, MAX_REQUEST)
            result = receive(stream, 65536)
        if isinstance(result, dict) and result.get('ok') is True and isinstance(result.get('response'), str):
            return result['response']
    except (OSError, ValueError, TypeError):
        pass
    return None
