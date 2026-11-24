"""Compare two drive outputs; print a summary of differing scenarios by first differing key."""
import json, sys, collections
a = {json.loads(l)["index"]: json.loads(l) for l in open(sys.argv[1])}
b = {json.loads(l)["index"]: json.loads(l) for l in open(sys.argv[2])}
def first_diff(x, y, path=""):
    if type(x) != type(y):
        return path, x, y
    if isinstance(x, dict):
        for k in sorted(set(x) | set(y), key=str):
            if x.get(k) != y.get(k):
                return first_diff(x.get(k), y.get(k), f"{path}.{k}")
    elif isinstance(x, list):
        if len(x) != len(y):
            for i, (p, q) in enumerate(zip(x, y)):
                if p != q:
                    return first_diff(p, q, f"{path}[{i}]")
            return f"{path}#len", len(x), len(y)
        for i, (p, q) in enumerate(zip(x, y)):
            if p != q:
                return first_diff(p, q, f"{path}[{i}]")
    elif x != y:
        return path, x, y
    return None
kinds = collections.Counter(); samples = {}
fatal = sum(1 for v in a.values() if "fatal" in v)
for i in sorted(a):
    if a[i] != b.get(i):
        d = first_diff(a[i], b.get(i))
        import re
        key = re.sub(r"\[\d+\]", "[]", d[0]) if d else "?"
        kinds[key] += 1
        samples.setdefault(key, (i, d))
print("scenarios", len(a), "fatal", fatal, "differ", sum(kinds.values()))
for k, n in kinds.most_common():
    i, d = samples[k]
    print(n, k, "e.g. index", i)
    print("   A:", str(d[1])[:400]); print("   B:", str(d[2])[:400])
