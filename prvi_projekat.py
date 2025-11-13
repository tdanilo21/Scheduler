from dataclasses import dataclass
import os
from enum import Enum, auto
import threading
from collections import deque

directory = ""

@dataclass
class Resources:
    CPU: int
    RAM: float

class Action:
    def __init__(self, cmd):
        self.cmd = cmd

    def run(self):
        pass

class ShellAction(Action):
    def __init__(self, cmd):
        super().__init__(cmd)
    
    def run(self):
        import subprocess
        return subprocess.run(self.cmd, shell=True, cwd=directory, capture_output=True, text=True)
        
class ScriptAction(ShellAction):
    def __init__(self, cmd):
        super().__init__("start /min cmd /c " + cmd)

class PythonAction(Action):
    def __init__(self, cmd):
        super().__init__(cmd)

    def run(self):
        import subprocess
        return subprocess.run(["py", self.cmd], cwd=directory, capture_output=True, text=True)

class CAction(Action):
    def __init__(self, cmd):
        super().__init__(cmd)

    def run(self):
        import subprocess
        import shutil

        compiler = shutil.which("gcc") or shutil.which("clang")
        if not compiler:
            raise EnvironmentError("No C compiler found in PATH.")
        base, _ = os.path.splitext(self.cmd)
        executable = base + (".exe" if os.name == "nt" else "")
        result = subprocess.run([compiler, self.cmd, "-o", executable], cwd=directory, capture_output=True, text=True)
        if result.returncode != 0:
            return result
        return subprocess.run([("" if os.name == "nt" else "./") + executable], cwd=directory, shell=True, capture_output=True, text=True)

class ActionFactory:
    @staticmethod
    def create_action(action_type, cmd):
        match action_type:
            case "shell":
                return ShellAction(cmd)
            case "script":
                return ScriptAction(cmd)
            case "python":
                return PythonAction(cmd)
            case "c":
                return CAction(cmd)
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

    def add_edge(self, node):
        self.edges.append(node)
        node.inverted_edges.append(self)
        node.degree += 1

    def update(self):
        self.mutex.acquire()
        self.degree -= 1
        if self.degree == 0:
            self.state = State.READY
            self.mutex.release()
            return True
        self.mutex.release()
        return False

    def try_build(self):
        self.mutex.acquire()
        if self.degree == 0 and (self.state == State.PENDING or self.state == State.FAILED):
            self.state = State.READY
            self.mutex.release()
            return True
        self.mutex.release()
        return False
    
    def mark_running(self):
        self.mutex.acquire()
        self.state = State.RUNNING
        self.mutex.release()

    def clean(self):
        self.mutex.acquire()
        self.state = State.PENDING
        self.degree = len(self.inverted_edges)
        self.mutex.release()
        for output in self.outputs:
            output_path = os.path.join(directory, output)
            if os.path.isfile(output_path):
                os.remove(output_path)
            elif os.path.isdir(output_path):
                import shutil
                shutil.rmtree(output_path)

    def read_state(self):
        self.mutex.acquire()
        state = self.state
        self.mutex.release()
        return state

    def print_node(self):
        print(f"Node ID: {self.id}")
        print(f"Action: {self.action}")
        print(f"Outputs: {self.outputs}")
        print(f"Resources: {self.resources}")
        print("Edges to:")
        for edge in self.edges:
            print(f"  - Node ID: {edge.id}")

class RAFThreadPool:
    def __init__(self, max_workers):
        self.max_workers = max_workers
        self.workers = []
        self.active_workers = 0
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

        def execute(self):
            result = self.func(*self.args)
            if result.returncode == 0:
                if self.callback:
                    self.callback(result, *self.callback_args)
            else:
                if self.err_callback:
                    self.err_callback(result, *self.err_callback_args)

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
            task.execute()
            
    def apply_async(self, func, args=(), callback=None, callback_args=(), err_callback=None, err_callback_args=()):
        if self.closed:
            raise RuntimeError("Pool has been closed.")
        task = self.Task(func, args, callback, callback_args, err_callback, err_callback_args)
        self.queue_lock.acquire()
        self.queue.append(task)
        self.queue_lock.release()
        self.semaphore.release()

    def close(self):
        self.closed = True

    def join(self):
        if not self.closed:
            raise RuntimeError("Cannot join on an open pool.")
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
    
    def is_closed(self):
        return self.closed

pool = RAFThreadPool(max_workers=10)
nodes = {}
capacity = {}
free_resources = {}
resources_lock = threading.Lock()
targets = []
planner_threads = {}

def load(file_path):
    global nodes, capacity, free_resources, targets, directory
    from pathlib import Path
    directory = Path(file_path).parent.resolve()
    import json
    with open(file_path, 'r') as file:
        data = json.load(file)
        capacity = Resources(data['capacity']['CPU'], data['capacity']['RAM'])
        free_resources = Resources(data['capacity']['CPU'], data['capacity']['RAM'])
        targets = data['targets']
        for node_data in data['nodes']:
            node = Node(
                id=node_data['id'],
                action=ActionFactory.create_action(node_data['action']['type'], node_data['action']['cmd']) if node_data['action'] else None,
                outputs=node_data['outputs'],
                resources=Resources(node_data['resources']['CPU'], node_data['resources']['RAM'])
            )
            if node.resources.CPU > capacity.CPU or node.resources.RAM > capacity.RAM:
                nodes = {}
                capacity = {}
                targets = []
                directory = ""
                return False
            nodes[node.id] = node
        for node_data in data['nodes']:
            node = nodes[node_data['id']]
            for neighbor_id in node_data['deps']:
                neighbor = nodes[neighbor_id]
                neighbor.add_edge(node)
    return True

class PlannerThread(threading.Thread):
    counter = 0
    counter_lock = threading.Lock()

    def __init__(self, target_id):
        super().__init__(target=self.build)
        self.target_id = target_id
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

        target = nodes[self.target_id]
        q = deque()
        self.deps.add(target)
        q.append(target)
        while q:
            s = q.popleft()
            for u in s.inverted_edges:
                if u not in self.deps:
                    self.deps.add(u)
                    q.append(u)

    def taks_completed(self, _, node):
        print(f"{node.id} completed.")
        self.task_executed(_, node)
    
    def task_failed(self, _, node):
        print(f"{node.id} failed.")
        self.task_executed(_, node)
    
    def task_executed(self, _, node):
        resources_lock.acquire()
        free_resources.CPU += node.resources.CPU
        free_resources.RAM += node.resources.RAM
        resources_lock.release()
        for thread in planner_threads.values():
            thread.notification_queue_lock.acquire()
            thread.notification_queue.append(node)
            thread.notification_queue_lock.release()
            thread.notification_queue_semaphore.release()

    def process_notification(self, node):
        if node.read_state() == State.FAILED or node.id == self.target_id:
            self.finished = True
            return
        for neighbor in node.edges:
            if (neighbor in self.deps 
                and ((node in self.executed_nodes and neighbor.update()) 
                or (node not in self.executed_nodes and neighbor.try_build()))):
                self.node_queue.append(neighbor)

    def build(self):
        if self.target_id not in nodes.keys():
            return False
        for node in self.deps:
            if node.try_build():
                self.node_queue.append(node)
        while not self.finished:
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
                node.mark_running()
                pool.apply_async(
                    func=node.action.run,
                    callback=self.taks_completed,
                    callback_args=(node,),
                    err_callback=self.task_failed,
                    err_callback_args=(node,)
                )
                self.executed_nodes.add(node)
            self.notification_queue_semaphore.acquire()
            self.notification_queue_lock.acquire()
            while self.notification_queue:
                node = self.notification_queue.popleft()
                self.notification_queue_lock.release()
                if node in self.deps:
                    self.process_notification(node)
                self.notification_queue_lock.acquire()
            self.notification_queue_lock.release()
        planner_threads.pop(self.id)

def clean(target_id=None):
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

while True:
    cmd = input("> ").strip().split(maxsplit=1)
    match cmd[0]:
        case "load":
            if load(cmd[1]):
                print("Project loaded successfully.")
            else:
                print("Failed to load project.")
        case "build":
            thread = PlannerThread(cmd[1])
            planner_threads[thread.id] = thread
            thread.start()
        case "clean":
            if planner_threads:
                print("Builds are in progress. Clean aborted.")
                continue
            confirmation = input("Are you sure you want to clean all outputs? (y/n): ").strip().lower()
            if confirmation != 'y':
                print("Clean aborted.")
                continue
            clean(cmd[1] if len(cmd) > 1 else None)
            print("Outputs cleaned.")
        case "exit":
            pool.close()
            pool.join()
            break
        case _:
            print("Unknown command.")