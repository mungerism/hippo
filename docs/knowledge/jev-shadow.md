# Jev Cold Path 显式旁路观察

#63 只增加显式注入式旁路，不提供默认出网开关。调用方必须先确认项目的事实文本允许发给 TypeSafe，随后自行构造客户端并指定精确项目 allowlist：

```python
import os

from hippo_memory.consolidator import MemoryConsolidator
from hippo_memory.jev import JevClient
from hippo_memory.jev_shadow import JevShadowPolicy

client = JevClient(api_key=os.environ["TYPESAFE_API_KEY"])
try:
    result = MemoryConsolidator(
        engine,
        shadow_backend=client,
        shadow_policy=JevShadowPolicy(
            allowed_projects=frozenset({"approved-project-id"}),
            max_calls=50,
            max_elapsed_seconds=30,
        ),
    ).consolidate(scope="project", project_id="approved-project-id", dry_run=True)
    shadow_report = result.shadow
finally:
    client.close()
```

`--dry-run` 只禁止记忆 mutation，**不禁止 Jev 出网**。没有显式注入时，原 Cold Path 和 CLI 完全不变；未列入 allowlist 或 `global` 范围时旁路不会调用后端。记录在调用前再次验证两侧用户/项目身份与活跃状态，且只向 Jev 传两侧事实文本；主判与旁路使用同一份读取快照。

`ConsolidationResult.shadow` 提供状态 `ok`、`not_allowed` 或 `degraded`、尝试/成功/失败/预算跳过数及逐对差异。每对只有运行内随机盐生成的 fingerprint、两个关系类别、Jev 概率/供应商 confidence、模型与 rubric 版本、耗时或安全失败代码；不含事实原文、记忆 ID 或 API 密钥。预算耗尽、超时与服务错误只降低旁路状态，现有主判仍可照常规划和执行。报告是一次运行内的审计结果，调用方应根据自己的保留策略保存；不要把报告中的 Jev 选择反向写入治理决策。
