"""Read-only, version-labelled Spool observations without creating storage."""

from collections import Counter
import json
from pathlib import Path


def collect_spool_checks(root: Path) -> list[dict]:
    checks = []
    for version in (1, 2):
        base = root if version == 1 else root / 'v2'
        category = f'Spool v{version}'

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({'category': category, 'name': name, 'ok': ok, 'detail': detail})

        try:
            if not base.exists():
                add('队列目录', True, '未发现目录（未启用）；未创建目录')
                continue
            held = False
            try:
                import fcntl
                with (base / 'worker.lock').open('rb') as stream:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        held = True
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                add('当前消费锁', True, '有消费者持锁（此版本有消费者）' if held else '未持锁（本次采样未观察到消费者）')
            except FileNotFoundError:
                add('当前消费锁', True, '锁文件不存在（本次采样未观察到消费者）')
            except (OSError, ImportError):
                add('当前消费锁', False, '无法探测消费锁，未判断此版本是否活跃')

            counts = Counter()
            invalid = 0
            jobs = base / 'jobs'
            if jobs.exists():
                for entry in jobs.iterdir():
                    if not entry.is_dir():
                        continue
                    try:
                        state = json.loads((entry / 'state.json').read_text()).get('state')
                        if state not in ('pending', 'processing', 'completed', 'skipped', 'coalesced', 'dead'):
                            raise ValueError('unknown state')
                        counts[state] += 1
                    except (OSError, ValueError, AttributeError, TypeError):
                        invalid += 1
            add('作业统计', invalid == 0,
                f"pending={counts['pending']}, processing={counts['processing']}, completed={counts['completed']}, "
                f"skipped={counts['skipped']}, coalesced={counts['coalesced']}, dead={counts['dead']}; 损坏/不可读={invalid}")
            add('待消费积压', counts['pending'] < 10,
                f"当前 pending: {counts['pending']} 个；{'有消费者持锁' if held else '未确认此版本有消费者'}；"
                f'查看 hippo hook status --queue-version {version}')
            add('死信作业 (dead)', counts['dead'] == 0,
                f"当前 dead: {counts['dead']} 个" +
                (f'；先只读审计 hippo hook audit-dead --queue-version {version}，不要盲目批量重试' if counts['dead'] else ''))
            receipts = base / 'receipts'
            receipt_count = sum(p.is_file() for p in receipts.glob('*.json')) if receipts.exists() else 0
            add('成功收据文件数', True, f'{receipt_count} 个（不等同于有效记忆数）')
        except OSError:
            add('队列状态', False, '队列元数据不可读；未修改队列')
    return checks
