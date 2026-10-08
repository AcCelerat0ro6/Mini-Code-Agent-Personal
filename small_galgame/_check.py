import ast
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

for p in sorted(pathlib.Path(".").glob("*.py")):
    tree = ast.parse(p.read_text(encoding="utf-8"))
    funcs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    print(p.name, "->", funcs)
