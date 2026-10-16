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


def run_capture(*, host, cwd=None, queue_version=1, sync=False, spool_dir=None):
    import os
    from pathlib import Path
    from hippo_memory.hooks import SpoolStorage, get_adapter

    if spool_dir is None:
        spool_dir = Path(os.environ.get('HIPPO_HOME', '~/.hippo')).expanduser() / 'spool'
    raw_input = ''
    try:
        if not sys.stdin.isatty():
            raw_input = sys.stdin.read()
    except Exception:
        pass
    adapter = payload = None
    try:
        try:
            adapter = get_adapter(host)
        except ValueError:
            sys.stderr.write('Hippo Hook warning: unknown host\n')
            return
        payload = adapter.parse_context(raw_input, env_cwd=cwd)
        payload.schema_version = queue_version
        if not payload.workspace_candidates:
            payload.workspace_candidates = [payload.project_dir]
        storage = SpoolStorage(base_dir=spool_dir, version=queue_version)
        if not storage.capture_allowed(payload):
            return
        if queue_version == 2 and SpoolStorage(base_dir=spool_dir).is_worker_active():
            raise RuntimeError('旧队列 Worker 仍在运行，升级切换完成前不能发布 v2 作业')
        storage.enqueue(payload)
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
        # Opt-out config and paths may contain private data. Match the legacy
        # fail-safe behavior without copying raw exception text into host logs.
        message = str(exc) if isinstance(exc, RuntimeError) and '旧队列 Worker' in str(exc) else type(exc).__name__
        sys.stderr.write(f'Hippo Hook capture error: {message}\n')
    finally:
        try:
            response = adapter.format_response(payload) if adapter and payload else '{}\n'
            sys.stdout.write(response)
            sys.stdout.flush()
        except Exception:
            try:
                sys.stdout.write('{}\n')
                sys.stdout.flush()
            except Exception:
                pass
