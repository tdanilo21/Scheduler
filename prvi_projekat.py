from dataclasses import dataclass
import os

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
        subprocess.run(self.cmd, shell=True, cwd=directory)
        
class ScriptAction(ShellAction):
    def __init__(self, cmd):
        super().__init__("start /min cmd /c " + cmd)

class PythonAction(Action):
    def __init__(self, cmd):
        super().__init__(cmd)

    def run(self):
        import subprocess
        subprocess.run(["py", self.cmd], shell=True, cwd=directory)

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
        executable = base + (".exe" if os.name == 'nt' else "")
        result = subprocess.run([compiler, self.cmd, "-o", executable], shell=True, cwd=directory)
        if result.returncode != 0:
            raise RuntimeError("Compilation failed.")
        subprocess.run([executable], shell=True, cwd=directory)

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

class Node:
    def __init__(self, id, action, outputs, resources):
        self.id = id
        self.action = action
        self.outputs = outputs
        self.resources = resources
        self.edges = []
        self.inverted_edges = []

    def add_edge(self, node):
        self.edges.append(node)
        node.inverted_edges.append(self)

    def print_node(self):
        print(f"Node ID: {self.id}")
        print(f"Action: {self.action}")
        print(f"Outputs: {self.outputs}")
        print(f"Resources: {self.resources}")
        print("Edges to:")
        for edge in self.edges:
            print(f"  - Node ID: {edge.id}")

nodes = {}
capacity = {}
targets = []

def load(file_path):
    global nodes, capacity, targets, directory
    from pathlib import Path
    directory = Path(file_path).parent.resolve()
    import json
    with open(file_path, 'r') as file:
        data = json.load(file)
        capacity = Resources(data['capacity']['CPU'], data['capacity']['RAM'])
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

def get_dependencies(target):
    visited = {node_id: False for node_id in nodes.keys()}
    from collections import deque
    q = deque()
    visited[target.id] = True
    q.append(target)
    while q:
        s = q.popleft()
        for u in s.inverted_edges:
            if not visited[u.id]:
                visited[u.id] = True
                q.append(u)
    return visited

def build(target):
    if target not in nodes.keys():
        return False
    s = nodes[target]
    visited = get_dependencies(s)
    from collections import deque
    q = deque()
    deg = {node_id: 0 for node_id, v in visited.items() if v}
    for node_id, v in visited.items():
        if v:
            deg[node_id] = len(nodes[node_id].inverted_edges)
            if deg[node_id] == 0:
                q.append(nodes[node_id])
    while q:
        u = q.popleft()
        if u.action:
            print(f"Running action for node {u.id}...")
            u.action.run()
        for v in u.edges:
            if visited[v.id]:
                deg[v.id] -= 1
                if deg[v.id] == 0:
                    q.append(v)
    return True

def clear():
    for node in nodes.values():
        for output in node.outputs:
            output_path = os.path.join(directory, output)
            if os.path.isfile(output_path):
                os.remove(output_path)
            elif os.path.isdir(output_path):
                import shutil
                shutil.rmtree(output_path)

while True:
    cmd = input("> ").strip().split(maxsplit=1)
    match cmd[0]:
        case "load":
            if load(cmd[1]):
                print("Project loaded successfully.")
            else:
                print("Failed to load project.")
        case "build":
            if build(cmd[1]):
                print("Build completed successfully.")
            else:
                print("Build failed.")
        case "clear":
            clear()
            print("Outputs cleared.")
        case "exit":
            break
        case _:
            print("Unknown command.")