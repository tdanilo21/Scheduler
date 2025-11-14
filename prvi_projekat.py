from dataclasses import dataclass
import os
from enum import Enum, auto
import threading
from collections import deque
import multiprocessing

@dataclass
class Resources:
    CPU: int
    RAM: float

class Action:
    def __init__(self, cmd, cwd):
        self.cmd = cmd
        self.cwd = cwd

    def run(self):
        pass

class ShellAction(Action):
    def __init__(self, cmd, cwd):
        super().__init__(cmd, cwd)
    
    def run(self):
        import subprocess
        result = subprocess.run(self.cmd, shell=True, cwd=self.cwd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(result.stderr)
        return result.stdout

class PythonAction(Action):
    def __init__(self, cmd, cwd):
        super().__init__(cmd, cwd)

    def run(self):
        import subprocess
        import shutil
        compiler = shutil.which("py") or shutil.which("python")
        if not compiler:
            raise EnvironmentError("No python compiler found in PATH")
        result = subprocess.run([compiler, self.cmd], cwd=self.cwd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(result.stderr)
        return result.stdout

class CAction(Action):
    def __init__(self, cmd, cwd):
        super().__init__(cmd, cwd)

    def run(self):
        import subprocess
        import shutil

        compiler = shutil.which("gcc") or shutil.which("clang")
        if not compiler:
            raise EnvironmentError("No C compiler found in PATH")
        base, _ = os.path.splitext(self.cmd)
        executable = base + (".exe" if os.name == "nt" else "")
        result = subprocess.run([compiler, self.cmd, "-o", executable], cwd=self.cwd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(result.stderr)
        result = subprocess.run([("" if os.name == "nt" else "./") + executable], cwd=self.cwd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(result.stderr)
        return result.stdout

class ActionFactory:
    @staticmethod
    def create_action(action_type, cmd, cwd):
        match action_type:
            case "shell":
                return ShellAction(cmd, cwd)
            case "python":
                return PythonAction(cmd, cwd)
            case "c":
                return CAction(cmd, cwd)
            case _:
                return None
            
class State(Enum):
    PENDING = auto()
    READY = auto()
    RUNNING = auto()
    DONE = auto()
    FAILED = auto()

class Node:
    def __init__(self, id, action, outputs, resources):
        self.id = id
        self.action = action
        self.outputs = outputs
        self.resources = resources
        self.edges = []
        self.inverted_edges = []
        self.state = State.PENDING
        self.degree = 0
        self.mutex = threading.Lock()
        self.error_message = None
        self.error_message_lock = threading.Lock()

    def add_edge(self, node):
        self.edges.append(node)
        node.inverted_edges.append(self)
        node.degree += 1

    def update(self, delta):
        self.mutex.acquire()
        self.degree -= delta
        if self.degree == 0:
            self.state = State.READY
        self.mutex.release()

    def try_build(self):
        self.mutex.acquire()
        if self.state == State.READY or self.state == State.FAILED:
            self.state = State.RUNNING
            self.mutex.release()
            return 1
        if self.state == State.DONE:
            self.mutex.release()
            return 2
        self.mutex.release()
        return 0
    
    def set_state(self, state):
        self.mutex.acquire()
        self.state = state
        self.mutex.release()

    def clean(self):
        self.mutex.acquire()
        self.state = State.PENDING if self.degree > 0 else State.READY
        self.mutex.release()
        self.error_message_lock.acquire()
        self.error_message = None
        self.error_message_lock.release()
        for output in self.outputs:
            output_path = os.path.join(self.action.cwd, output)
            if os.path.isfile(output_path):
                os.remove(output_path)
            elif os.path.isdir(output_path):
                import shutil
                shutil.rmtree(output_path)
        for neighbor in self.edges:
            neighbor.mutex.acquire()
            neighbor.degree += 1
            neighbor.mutex.release()

    def read_state(self):
        self.mutex.acquire()
        state = self.state
        self.mutex.release()
        return state
    
    def read_degree(self):
        self.mutex.acquire()
        degree = self.degree
        self.mutex.release()
        return degree
    
    def set_error_message(self, message):
        self.error_message_lock.acquire()
        self.error_message = message
        self.error_message_lock.release()

    def get_error_message(self):
        self.error_message_lock.acquire()
        message = self.error_message
        self.error_message_lock.release()
        return message
    
class Future:
    def __init__(self):
        self.res_event = threading.Event()
        self.exc_event = threading.Event()
        self.res = None
        self.exc = None

    def set_result(self, res):
        self.res = res
        self.res_event.set()

    def set_exception(self, exc):
        self.exc = exc
        self.exc_event.set()

    def result(self, timeout=None):
        flag = self.res_event.wait(timeout)
        if flag:
            return self.res
        raise TimeoutError()
    
    def exception(self, timeout=None):
        flag = self.exc_event.wait(timeout)
        if flag:
            return self.exc
        raise TimeoutError()
    
    def done(self):
        return self.res_event.is_set()

class RAFThreadPool:
    def __init__(self, max_workers):
        self._max_workers = max_workers
        self.workers = []
        self.active_workers = 0
        self.active_workers_lock = threading.Lock()
        self.queue = deque()
        self.queue_lock = threading.Lock()
        self.semaphore = threading.Semaphore(0)
        self.closed = False
        for _ in range(max_workers):
            worker_thread = threading.Thread(target=self.worker)
            worker_thread.start()
            self.workers.append(worker_thread)

    class Task:
        def __init__(self, func, args=(), callback=None, callback_args=(), err_callback=None, err_callback_args=()):
            self.func = func
            self.args = args
            self.callback = callback
            self.callback_args = callback_args
            self.err_callback = err_callback
            self.err_callback_args = err_callback_args
            self.future = Future()

        def execute(self):
            try:
                result = self.func(*self.args)
                self.future.set_result(result)
                self.future.set_exception(None)
                if self.callback:
                    self.callback(result, *self.callback_args)
            except Exception as e:
                self.future.set_exception(str(e))
                self.future.set_result(None)
                if self.err_callback:
                    self.err_callback(str(e), *self.err_callback_args)
            

    def worker(self):
        while True:
            if not self.semaphore.acquire(timeout=1):
                if self.closed:
                    break
                else:
                    continue
            self.queue_lock.acquire()
            task = self.queue.popleft()
            self.queue_lock.release()
            self.active_workers_lock.acquire()
            self.active_workers += 1
            self.active_workers_lock.release()
            task.execute()
            self.active_workers_lock.acquire()
            self.active_workers -= 1
            self.active_workers_lock.release()
            
    def apply_async(self, func, args=(), callback=None, callback_args=(), err_callback=None, err_callback_args=()):
        if self.closed:
            raise RuntimeError("Pool has been closed")
        task = self.Task(func, args, callback, callback_args, err_callback, err_callback_args)
        self.queue_lock.acquire()
        self.queue.append(task)
        self.queue_lock.release()
        self.semaphore.release()
        return task.future

    def close(self):
        self.closed = True

    def join(self):
        if not self.closed:
            raise RuntimeError("Cannot join on an open pool")
        for worker in self.workers:
            worker.join()

    def terminate(self):
        self.queue_lock.acquire()
        while self.queue:
            task = self.queue.popleft()
            if task.err_callback:
                task.err_callback("Pool has been terminated", *task.err_callback_args)
        self.queue_lock.release()

    def num_active_workers(self):
        return self.active_workers

    def max_workers(self):
        return self._max_workers
    
    def is_closed(self):
        return self.closed
    
def wrapper_task(func, args, active_workers, active_workers_lock):
    active_workers_lock.acquire()
    active_workers.value += 1
    active_workers_lock.release()
    result = func(*args)
    active_workers_lock.acquire()
    active_workers.value -= 1
    active_workers_lock.release()
    return result
    
class RAFProcessPoll():
    def __init__(self, max_workers=None):
        self.pool = multiprocessing.Pool(processes=max_workers)
        self.closed = False
        manager = multiprocessing.Manager()
        self.active_workers = manager.Value('i', 0)
        self.active_workers_lock = manager.Lock()

    def apply_async(self, func, args=(), callback=None, callback_args=(), err_callback=None, err_callback_args=()):
        self.pool.apply_async(
            func=wrapper_task,
            args=(func, args, self.active_workers, self.active_workers_lock),
            callback=lambda res: callback(res, *callback_args),
            error_callback=lambda exc: err_callback(exc, *err_callback_args)
        )

    def close(self):
        self.pool.close()
        self.closed = True

    def join(self):
        self.pool.join()

    def terminate(self):
        self.pool.terminate()

    def num_active_workers(self):
        return self.active_workers.value
    
    def max_workers(self):
        return self.pool._processes
    
    def is_closed(self):
        return self.is_closed
    
if __name__ == "__main__":
    pool = RAFThreadPool(10)
    nodes = {}
    capacity = {}
    free_resources = {}
    resources_lock = threading.Lock()
    targets = []
    planner_threads = {}
    planner_threads_lock = threading.Lock()
    stop_event = threading.Event()

    print_queue = deque()
    print_queue_lock = threading.Lock()
    print_queue_semaphore = threading.Semaphore(0)

def print_thread_safe(message):
    print_queue_lock.acquire()
    print_queue.append(message)
    print_queue_lock.release()
    print_queue_semaphore.release()

def load(file_path):
    global nodes, capacity, free_resources, targets
    from pathlib import Path
    directory = Path(file_path).parent.resolve()
    import json
    try:
        with open(file_path, 'r') as file:
            data = json.load(file)
            capacity = Resources(data['capacity']['CPU'], data['capacity']['RAM'])
            free_resources = Resources(data['capacity']['CPU'], data['capacity']['RAM'])
            targets = data['targets']
            for node_data in data['nodes']:
                node = Node(
                    id=node_data['id'],
                    action=ActionFactory.create_action(node_data['action']['type'], node_data['action']['cmd'], directory) if node_data['action'] else None,
                    outputs=node_data['outputs'],
                    resources=Resources(node_data['resources']['CPU'], node_data['resources']['RAM'])
                )
                if node.resources.CPU > capacity.CPU or node.resources.RAM > capacity.RAM:
                    print_thread_safe(f"There is not enough resources for node {node.id}")
                    print_thread_safe(None)
                    nodes = {}
                    capacity = {}
                    targets = []
                    directory = ""
                    return
                nodes[node.id] = node
            print_thread_safe(None)
            for node_data in data['nodes']:
                node = nodes[node_data['id']]
                for neighbor_id in node_data['deps']:
                    neighbor = nodes[neighbor_id]
                    neighbor.add_edge(node)
        for node in nodes.values():
            node.update(0)
    except FileNotFoundError as e:
        print_thread_safe(str(e))
        print_thread_safe(None)

class PlannerThread(threading.Thread):
    counter = 0
    counter_lock = threading.Lock()

    def __init__(self, target_id, stop_event, p):
        super().__init__(target=self.build)
        self.target_id = target_id
        self.stop_event = stop_event
        self.p = p
        PlannerThread.counter_lock.acquire()
        self.id = PlannerThread.counter
        PlannerThread.counter += 1
        PlannerThread.counter_lock.release()
        self.notification_queue = deque()
        self.notification_queue_lock = threading.Lock()
        self.notification_queue_semaphore = threading.Semaphore(0)
        self.node_queue = deque()
        self.executed_nodes = set()
        self.deps = set()
        self.finished = False
        self.unknown = False
        if target_id not in nodes.keys():
            self.unknown = True
            return
        self.target = nodes[self.target_id]

        q = deque()
        self.deps.add(self.target)
        q.append(self.target)
        while q:
            s = q.popleft()
            for u in s.inverted_edges:
                if u not in self.deps:
                    self.deps.add(u)
                    q.append(u)

    def taks_completed(self, _, node):
        node.set_state(State.DONE)
        if self.p:
            print_thread_safe(f"{node.id} completed")
        for neighbor in node.edges:
            neighbor.update(1)
        self.task_executed(node)
    
    def task_failed(self, result, node):
        node.set_state(State.FAILED)
        if self.p:
            print_thread_safe(f"{node.id} failed with error:\n{result}")
        node.set_error_message(result)
        self.task_executed(node)
    
    def task_executed(self, node):
        resources_lock.acquire()
        free_resources.CPU += node.resources.CPU
        free_resources.RAM += node.resources.RAM
        resources_lock.release()
        for thread in planner_threads.values():
            thread.notification_queue_lock.acquire()
            thread.notification_queue.append(node)
            thread.notification_queue_lock.release()
            thread.notification_queue_semaphore.release()

    def node_queue_append(self, node):
        if not node.action:
            self.taks_completed(None, node)
        else:
            self.node_queue.append(node)

    def process_notification(self, node):
        if node.read_state() == State.FAILED or node.id == self.target_id:
            self.finished = True
        for neighbor in node.edges:
            if neighbor in self.deps:
                result = neighbor.try_build()
                if result == 1:
                    self.node_queue_append(neighbor)
                elif result == 2:
                    self.process_notification(neighbor)

    def remove(self):
        planner_threads_lock.acquire()
        planner_threads.pop(self.id)
        planner_threads_lock.release()

    def send_tasks(self):
        while self.node_queue:
            node = self.node_queue.popleft()
            resources_lock.acquire()
            if node.resources.CPU > free_resources.CPU or node.resources.RAM > free_resources.RAM:
                resources_lock.release()
                self.node_queue.appendleft(node)
                break
            free_resources.CPU -= node.resources.CPU
            free_resources.RAM -= node.resources.RAM
            resources_lock.release()
            try:
                pool.apply_async(
                    func=node.action.run,
                    callback=self.taks_completed,
                    callback_args=(node,),
                    err_callback=self.task_failed,
                    err_callback_args=(node,)
                )
                self.executed_nodes.add(node)
            except RuntimeError:
                self.finished = True

    def build(self):
        if self.unknown:
            print_thread_safe(f"Unknown target: {self.target_id}")
            print_thread_safe(None)
            self.remove()
            return
        if not self.p:
            print_thread_safe(None)
        for node in self.deps:
            if node.try_build() == 1:
                self.node_queue_append(node)
        if self.target.read_state() == State.DONE:
            self.finished = True
        self.send_tasks()
        while not self.finished:
            if not self.notification_queue_semaphore.acquire(timeout=1):
                if self.stop_event.is_set():
                    break
                else:
                    continue
            if self.stop_event.is_set():
                break
            self.notification_queue_lock.acquire()
            while self.notification_queue:
                node = self.notification_queue.popleft()
                self.notification_queue_lock.release()
                if node in self.deps:
                    self.process_notification(node)
                self.notification_queue_lock.acquire()
            self.notification_queue_lock.release()
            self.send_tasks()
        if self.p:
            print_thread_safe(None)
        self.remove()

def clean(target_id=None):
    print_thread_safe(None)
    queue = deque()
    visited = {id: False for id in nodes.keys()}
    if target_id:
        if target_id not in nodes:
            return
        queue.append(nodes[target_id])
        visited[target_id] = True
    else:
        for node in nodes.values():
            if not node.inverted_edges:
                queue.append(node)
                visited[node.id] = True
    while queue:
        node = queue.popleft()
        node.clean()
        for neighbor in node.edges:
            if not visited[neighbor.id]:
                visited[neighbor.id] = True
                queue.append(neighbor)

def num_active_planner_threads():
    planner_threads_lock.acquire()
    count = len(planner_threads)
    planner_threads_lock.release()
    return count

def stats():
    cnt = {state: 0 for state in State}
    for node in nodes.values():
        cnt[node.read_state()] += 1
    resources_lock.acquire()
    resources_in_use = Resources(
        CPU=capacity.CPU - free_resources.CPU,
        RAM=capacity.RAM - free_resources.RAM
    )
    resources_lock.release()
    current_planner_threads = num_active_planner_threads()

    print_thread_safe(f"Number of nodes: {len(nodes)}")
    for state in State:
        print_thread_safe(f"  {state.name}: {cnt[state]}")
    print_thread_safe(f"Resources in use: CPU {resources_in_use.CPU}/{capacity.CPU}, RAM {resources_in_use.RAM}/{capacity.RAM}")
    print_thread_safe(f"Active pool workers: {pool.num_active_workers()}/{pool.max_workers()}")
    print_thread_safe(f"Active planner threads: {current_planner_threads}")
    print_thread_safe(None)

def describe(node_id):
    if node_id not in nodes.keys():
        print_thread_safe(f"Unknown node: {node_id}")
        print_thread_safe(None)
        return
    node = nodes[node_id]
    inputs = ""
    for neighbor in node.inverted_edges:
        for output in neighbor.outputs:
            if len(inputs) > 0:
                inputs += ", "
            inputs += output
    outputs = ""
    for output in node.outputs:
        if len(outputs) > 0:
                outputs += ", "
        outputs += output
    error = node.get_error_message()
    
    print_thread_safe(f"State: {node.read_state().name}")
    print_thread_safe(f"Number of pending dependencies: {node.read_degree()}")
    print_thread_safe(f"Inputs: {inputs}")
    print_thread_safe(f"Outputs: {outputs}")
    print_thread_safe(f"Resources: CPU {node.resources.CPU}, RAM {node.resources.RAM}")
    if error:
        print_thread_safe(f"Last error message was:\n{error}")
    print_thread_safe(None)

def cancel():
    print_thread_safe(None)
    stop_event.set()
    pool.terminate()

def command_input():
    cmd = input("> ").strip().split(maxsplit=1)
    match cmd[0]:
        case "load":
            if len(cmd) == 1:
                print("Usage: load <path.json>")
                return 1
            thread = threading.Thread(target=load, args=(cmd[1],))
            thread.start()
        case "build":
            if len(cmd) == 1:
                print("Usage: build [options] <target_id>\nOptions:\n" \
                    "\t-p: print tasks when executed (I/O will be blocked for the duration of the build)")
                return 1
            p = False
            if cmd[1].startswith("-p "):
                temp = cmd[1].split(maxsplit=1)
                cmd[1] = temp[1]
                p = True
            if cmd[1].startswith("\"") and cmd[1].endswith("\""):
                cmd[1] = cmd[1][1:-1]
            thread = PlannerThread(cmd[1], stop_event, p)
            planner_threads_lock.acquire()
            planner_threads[thread.id] = thread
            planner_threads_lock.release()
            thread.start()
        case "clean":
            if num_active_planner_threads() > 0:
                print("Builds are in progress. Clean aborted")
                return 1
            confirmation = input("Are you sure you want to clean all outputs? (y/n): ").strip().lower()
            if confirmation != 'y':
                print("Clean aborted")
                return 1
            thread = threading.Thread(target=clean, args=(cmd[1] if len(cmd) > 1 else None,))
            thread.start()
        case "stats":
            thread = threading.Thread(target=stats)
            thread.start()
        case "describe":
            if len(cmd) == 1:
                print("Usage: describe <node_id>")
                return 1
            thread = threading.Thread(target=describe, args=(cmd[1],))
            thread.start()
        case "cancel":
            if num_active_planner_threads() == 0:
                print("No builds are in progress")
                return 1
            thread = threading.Thread(target=cancel)
            thread.start()
        case "exit":
            if num_active_planner_threads() > 0:
                cancel()
            pool.close()
            pool.join()
            return 2
        case _:
            print("Unknown command")
            return 1
    return 0

def output():
    while True:
        print_queue_semaphore.acquire()
        print_queue_lock.acquire()
        while print_queue:
            message = print_queue.popleft()
            if not message:
                print_queue_lock.release()
                return
            print(message)
        print_queue_lock.release()

def console():
    while True:
        result = command_input()
        if result == 2:
            break
        if result == 1:
            continue
        output()

if __name__ == "__main__":
    console()