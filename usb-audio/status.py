"""桥状态文件：守护进程每轮统计原子写入，供控制面查询桥是否在用。

控制面按 updated_at 与 active 判定桥是否可用。写入失败只记录一次原因，
下次成功写入后清除该原因。
"""
import json
from pathlib import Path


class StatusWriter:
    def __init__(self, path):
        self.path = Path(path)
        self.error = ''

    def write(self, snapshot):
        """写入快照；返回 False 表示本次写入失败。"""
        temporary = self.path.with_suffix('.json.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(snapshot) + '\n')
            temporary.replace(self.path)
        except OSError as error:
            reason = f'状态文件写入失败: {error}'
            if reason != self.error:
                self.error = reason
                print(reason, flush=True)
            return False
        self.error = ''
        return True
