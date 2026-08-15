"""Lists the module-level (global) names each named top-level function in a
file actually references, so a proposed relocation of that function to another
module can be checked for hidden coupling mechanically instead of by eye.

Usage: check_function_globals.py <file.py> <func> [<func> ...]
"""
import ast
import sys


def globals_used(fn: ast.FunctionDef, module_names: set[str]) -> set[str]:
    bound: set[str] = set()

    def bind_target(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                bound.add(n.id)

    for arg in list(fn.args.args) + list(fn.args.kwonlyargs) + list(fn.args.posonlyargs):
        bound.add(arg.arg)
    if fn.args.vararg:
        bound.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        bound.add(fn.args.kwarg.arg)

    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            for t in ([node.target] if hasattr(node, 'target') and node.target else []) + \
                     list(getattr(node, 'targets', [])):
                bind_target(t)
        elif isinstance(node, (ast.For, ast.comprehension)):
            bind_target(node.target)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node is not fn:
                bound.add(node.name)
        elif isinstance(node, ast.Lambda):
            for arg in node.args.args:
                bound.add(arg.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split('.')[0])
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            bind_target(node.optional_vars)

    used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return {n for n in used - bound if n in module_names}


def main():
    path, targets = sys.argv[1], sys.argv[2:]
    tree = ast.parse(open(path).read())

    module_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                module_names.add((a.asname or a.name).split('.')[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets):
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        module_names.add(n.id)

    by_name = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for t in targets:
        fn = by_name.get(t)
        if fn is None:
            print(f"{t}: NOT FOUND as a top-level function")
            continue
        print(f"{t} (lines {fn.lineno}-{fn.end_lineno}): {sorted(globals_used(fn, module_names))}")


if __name__ == '__main__':
    main()
