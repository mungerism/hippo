"""Capture listener owned by a locked Worker daemon, using the ordinary durable queue."""

import logging
import os
import socket
import stat
import threading

from hippo_memory.capture_transport import MAX_REQUEST, receive, send, socket_path

logger = logging.getLogger(__name__)


class CaptureServer:
    def __init__(self, storage):
        self.storage = storage
        self.path = socket_path(storage.root_dir, storage.version)
        self.socket = None
        self.thread = None
        self.stopping = threading.Event()
        self.identity = None
        self.completed_requests = 0

    def start(self):
        """Caller must hold worker.lock for this queue until close() returns."""
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise OSError('Capture endpoint is not an owned socket')
            os.unlink(self.path)
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stream.bind(self.path)
            os.chmod(self.path, 0o600)
            info = os.lstat(self.path)
            self.identity = (info.st_dev, info.st_ino)
            stream.listen(16)
            stream.settimeout(0.1)
            self.socket = stream
            self.thread = threading.Thread(target=self._serve, name='hippo-capture', daemon=True)
            self.thread.start()
        except Exception:
            stream.close()
            self.close()
            raise
        return self

    def _serve(self):
        while not self.stopping.is_set():
            try:
                client, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with client:
                client.settimeout(0.25)
                try:
                    request = receive(client, MAX_REQUEST)
                    if not isinstance(request, dict) or set(request) != {'host', 'cwd', 'raw_input', 'version'}:
                        raise ValueError('Invalid capture request')
                    if (type(request['version']) is not int or request['version'] != self.storage.version
                            or not isinstance(request['host'], str) or not isinstance(request['raw_input'], str)
                            or not (request['cwd'] is None or isinstance(request['cwd'], str))):
                        raise ValueError('Invalid capture fields')
                    from hippo_memory.capture import publish_capture
                    response, _, _ = publish_capture(
                        host=request['host'], cwd=request['cwd'], raw_input=request['raw_input'],
                        queue_version=request['version'], spool_dir=self.storage.root_dir,
                    )
                    send(client, {'ok': True, 'response': response}, 65536)
                    self.completed_requests += 1
                except Exception as exc:
                    # No raw request, path or exception text in daemon logs.
                    logger.debug('Capture transport fallback: %s', type(exc).__name__)
                    try:
                        send(client, {'ok': False}, 65536)
                    except (OSError, ValueError):
                        pass

    def close(self):
        self.stopping.set()
        if self.socket is not None:
            self.socket.close()
        if self.thread is not None:
            self.thread.join()
        if self.identity is not None:
            try:
                info = os.lstat(self.path)
                if (info.st_dev, info.st_ino) == self.identity:
                    os.unlink(self.path)
            except FileNotFoundError:
                pass
