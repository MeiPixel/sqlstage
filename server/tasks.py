"""长任务管理：让自动寻优这类任务能中途停止。

同步端点跑在 uvicorn 的工作线程里，前端断开连接服务端是感知不到的，请求会照常
跑完。所以任务必须单独起线程并保留句柄，界面上的“停止”才有意义。停止发生在
组与组之间——已经提交给线程池的那一组会跑完，不会被丢弃。
"""
import threading
import traceback
import uuid
from datetime import datetime

MAX_KEEP = 50


def ensure_idle(tasks, kind):
    if any(task.kind == kind and task.status in {"pending", "running"} for task in tasks):
        raise ValueError("已有同类搜索在运行；请先等待完成或停止，避免重复付费")


class Task:
    def __init__(self, task_id, kind):
        self.id = task_id
        self.kind = kind
        self.status = "pending"
        self.message = ""
        self.table = []
        self.csv_path = None
        self.error = None
        self.detail = None
        self.started_at = datetime.now()
        self.finished_at = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def request_stop(self):
        self._stop.set()

    def stop_check(self):
        """交给 runner 的回调：每组开始前问一次是否该停。"""
        return self._stop.is_set()

    @property
    def stopped(self):
        return self._stop.is_set()

    def snapshot(self):
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "message": self.message,
                "table": self.table,
                "csv_path": self.csv_path,
                "error": self.error,
                "detail": self.detail,
                "started_at": self.started_at.isoformat(timespec="seconds"),
                "finished_at": self.finished_at.isoformat(timespec="seconds")
                if self.finished_at else None,
            }


class TaskManager:
    def __init__(self):
        self._tasks = {}
        self._lock = threading.Lock()

    def start(self, kind, runner, exclusive=False):
        """runner(should_stop) 需返回一个产生 (message, table, csv_path) 的迭代器。"""
        task = Task(uuid.uuid4().hex[:12], kind)

        def work():
            task.status = "running"
            try:
                for message, table, csv_path in runner(task.stop_check):
                    with task._lock:
                        task.message = message
                        if table:
                            task.table = table
                        if csv_path:
                            task.csv_path = csv_path
                    if task.stopped and message and message.startswith("已停止"):
                        break
            except Exception as error:  # noqa: BLE001
                with task._lock:
                    task.error = f"{type(error).__name__}: {error}"
                    task.detail = traceback.format_exc()
                    task.status = "error"
            else:
                with task._lock:
                    task.status = "stopped" if task.stopped else "done"
            finally:
                with task._lock:
                    task.finished_at = datetime.now()
                self._prune()

        with self._lock:
            if exclusive:
                ensure_idle(self._tasks.values(), kind)
            self._tasks[task.id] = task
        thread = threading.Thread(target=work, name=f"task-{task.id}", daemon=True)
        thread.start()
        return task

    def get(self, task_id):
        with self._lock:
            return self._tasks.get(task_id)

    def stop(self, task_id):
        task = self.get(task_id)
        if task is None:
            return None
        task.request_stop()
        return task

    def _prune(self):
        with self._lock:
            finished = [item for item in self._tasks.values()
                        if item.status in ("done", "stopped", "error")]
            finished.sort(key=lambda item: item.started_at)
            for item in finished[:-MAX_KEEP]:
                self._tasks.pop(item.id, None)


manager = TaskManager()
