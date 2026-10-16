"""Small capture entrypoint: parse, enforce opt-out, durably publish, exit.

Help, invalid options and --sync use the full CLI. The hot path never imports
Typer, Rich, backend configuration, the memory engine or external providers.
"""

import sys


def try_fast_capture(args) -> bool:
    if args[:2] != ['hook', 'capture'] or '--help' in args or '--sync' in args:
        return False
    import getopt

    flags = {'--host', '--cwd', '--queue-version'}
    if any(arg.startswith('--') and arg.split('=', 1)[0] not in flags for arg in args[2:]):
        return False
    try:
        options, positional = getopt.getopt(args[2:], 'h:', ['host=', 'cwd=', 'queue-version='])
        values = dict(options)
        host = next((value for key, value in reversed(options) if key in ('--host', '-h')), None)
        version = int(values.get('--queue-version', '1'))
        if positional or host is None or version not in (1, 2):
            return False
    except (ValueError, getopt.GetoptError):
        return False
    run_capture(host=host, cwd=values.get('--cwd'), queue_version=version)
    return True


def publish_capture(*, host, cwd, raw_input, queue_version, spool_dir):
    """Shared durable publication, including capture opt-outs and legacy barriers."""
    from hippo_memory.hooks import SpoolStorage, get_adapter

    adapter = get_adapter(host)
    payload = adapter.parse_context(raw_input, env_cwd=cwd)
    payload.schema_version = queue_version
    if not payload.workspace_candidates:
        payload.workspace_candidates = [payload.project_dir]
    storage = SpoolStorage(base_dir=spool_dir, version=queue_version)
    response = adapter.format_response(payload)
    if not storage.capture_allowed(payload):
        return response, None, storage
    if queue_version == 2 and SpoolStorage(base_dir=spool_dir).is_worker_active():
        raise RuntimeError('旧队列 Worker 仍在运行，升级切换完成前不能发布 v2 作业')
    storage.enqueue(payload)
    return response, payload, storage


def run_capture(*, host, cwd=None, queue_version=1, sync=False, spool_dir=None):
    import os

    if spool_dir is None:
        spool_dir = os.path.join(os.path.expanduser(os.environ.get('HIPPO_HOME', '~/.hippo')), 'spool')
    raw_input = ''
    try:
        if not sys.stdin.isatty():
            raw_input = sys.stdin.read()
    except Exception:
        pass
    response = '{}\n'
    try:
        if not sync:
            from hippo_memory.capture_transport import capture_via_worker
            remote = capture_via_worker(spool_dir=spool_dir, version=queue_version,
                                        host=host, cwd=cwd, raw_input=raw_input)
            if remote is not None:
                response = remote
                return
        response, payload, storage = publish_capture(
            host=host, cwd=cwd, raw_input=raw_input,
            queue_version=queue_version, spool_dir=spool_dir,
        )
        if payload is None:
            return
        if sync:
            from hippo_memory.hooks import SpoolWorker
            SpoolWorker(storage=storage).process_one_job(payload)
        elif not storage.is_worker_active():
            try:
                import subprocess
                subprocess.Popen(
                    [sys.executable, '-m', 'hippo_memory.cli', 'hook', 'worker', '--drain',
                     '--queue-version', str(queue_version)],
                    start_new_session=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                )
            except Exception:
                pass
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) and '旧队列 Worker' in str(exc) else type(exc).__name__
        sys.stderr.write(f'Hippo Hook capture error: {message}\n')
    finally:
        try:
            sys.stdout.write(response)
            sys.stdout.flush()
        except Exception:
            pass
