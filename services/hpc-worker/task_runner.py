#!/usr/bin/env python3
"""Executes one HPC task in an isolated subprocess.

Reads ``{"code": str, "args": {...}}`` as JSON on stdin. The code runs with
the args as variables and numpy pre-imported as ``np``. The value of the
LAST expression — or an explicit ``result = ...`` assignment — becomes the
task result. Prints a single result envelope line to stdout:

    @PHONOS_HPC_RESULT@{"ok": true, "result": ..., "stdout": "..."}

Everything the task prints is captured separately and returned in "stdout".
"""
import ast
import io
import json
import sys
import traceback

_ENVELOPE = "@PHONOS_HPC_RESULT@"

# Result serialization caps (protects the API and the notebook client).
MAX_SEQUENCE_ELEMENTS = 200_000
MAX_DEPTH = 12
MAX_REPR_CHARS = 20_000


def to_jsonable(value, depth=0):
    """Best-effort conversion to JSON-representable data."""
    if depth > MAX_DEPTH:
        return repr(value)[:MAX_REPR_CHARS]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        import numpy as np
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            if value.size > MAX_SEQUENCE_ELEMENTS:
                raise ValueError(
                    f"Result array has {value.size} elements "
                    f"(limit {MAX_SEQUENCE_ELEMENTS}). Reduce it on the "
                    f"worker (e.g. aggregate or slice) before returning."
                )
            return value.tolist()
    except ImportError:
        pass
    if isinstance(value, dict):
        return {str(k): to_jsonable(v, depth + 1) for k, v in list(value.items())[:MAX_SEQUENCE_ELEMENTS]}
    if isinstance(value, (list, tuple, set)):
        seq = list(value)
        if len(seq) > MAX_SEQUENCE_ELEMENTS:
            raise ValueError(
                f"Result sequence has {len(seq)} elements (limit {MAX_SEQUENCE_ELEMENTS})."
            )
        return [to_jsonable(v, depth + 1) for v in seq]
    # pandas objects: readable but bounded
    try:
        import pandas as pd
        if isinstance(value, pd.DataFrame):
            if len(value) * max(1, len(value.columns)) > MAX_SEQUENCE_ELEMENTS:
                raise ValueError("Result DataFrame too large; aggregate before returning.")
            return json.loads(value.to_json(orient="split"))
        if isinstance(value, pd.Series):
            return to_jsonable(value.to_list(), depth + 1)
    except ImportError:
        pass
    return repr(value)[:MAX_REPR_CHARS]


def main():
    payload = json.loads(sys.stdin.read() or "{}")
    code = payload.get("code") or ""
    args = payload.get("args") or {}

    import numpy as np
    namespace = {"np": np, "numpy": np, "__name__": "__phonos_hpc_task__"}
    namespace.update(args)

    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    ok, result, error = True, None, None
    try:
        tree = ast.parse(code, mode="exec")
        # If the final statement is an expression, evaluate it as the result
        # (IPython semantics). An explicit `result = ...` wins if set.
        last_expr = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last_expr = ast.Expression(tree.body[-1].value)
            tree.body = tree.body[:-1]
        exec(compile(tree, "<hpc-task>", "exec"), namespace)  # noqa: S102 - that's the job
        if last_expr is not None:
            result = eval(compile(last_expr, "<hpc-task>", "eval"), namespace)  # noqa: S307
        if "result" in namespace and namespace["result"] is not None:
            result = namespace["result"]
        result = to_jsonable(result)
    except Exception:
        ok = False
        error = traceback.format_exc(limit=20)
    finally:
        sys.stdout = real_stdout

    envelope = {"ok": ok, "result": result if ok else None,
                "error": error, "stdout": captured.getvalue()[:20000]}
    print(_ENVELOPE + json.dumps(envelope))


if __name__ == "__main__":
    main()
