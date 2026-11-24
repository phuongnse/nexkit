"""usage: compare.py OLDPREFIX NEWPREFIX N  -> prints differing scenarios grouped"""
import json, sys, collections
old, new, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
diffs = collections.Counter(); examples = {}
total = 0
def keys(a, b, path=""):
    if type(a) != type(b): return [path]
    if isinstance(a, dict):
        out=[]
        for k in set(a)|set(b):
            if a.get(k, "<missing>") != b.get(k, "<missing>"):
                out += keys(a.get(k), b.get(k), f"{path}.{k}")
        return out
    if isinstance(a, list) and len(a)==len(b):
        out=[]
        for i,(x,y) in enumerate(zip(a,b)):
            if x!=y: out += keys(x,y,f"{path}[{i}]")
        return out
    return [path] if a != b else []
for i in range(n):
    import gzip
    with gzip.open(f"{old}.{i}", "rt") as fo, gzip.open(f"{new}.{i}", "rt") as fn:
        for lo, ln in zip(fo, fn):
            total += 1
            import re
            R=re.compile(r"\d\d:\d\d UTC on \d{4}-\d\d-\d\d|\d{4}-\d\d-\d\d \d\d:\d\d UTC")
            a, b = json.loads(R.sub("TIME", lo)), json.loads(R.sub("TIME", ln))
            assert a["sc"] == b["sc"]
            if a != b:
                ks = tuple(sorted(set(keys({k:a[k] for k in a if k!="sc"}, {k:b[k] for k in b if k!="sc"}))))
                diffs[ks] += 1
                examples.setdefault(ks, (a["sc"], a, b))
print("total", total, "differing", sum(diffs.values()))
for ks, c in diffs.most_common():
    print(c, ks[:8])
    sc, a, b = examples[ks]
    print("   sc", sc)
json.dump({repr(k): [v[0], v[1], v[2]] for k, v in examples.items()}, open(f"{new}.examples.json", "w"))
