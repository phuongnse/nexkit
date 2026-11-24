"""What every harness shares: decoding markers, snapshots of a fake GitHub, sharding."""
import base64, gzip, json, os, re, sys

TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?")
MARK = re.compile(r"<!-- nexkit:([a-z-]+) ([A-Za-z0-9+/=]+) -->")
HUMAN = re.compile(r"\d\d:\d\d UTC on \d{4}-\d\d-\d\d|\d{4}-\d\d-\d\d \d\d:\d\d UTC")


def norm(text):
    def dec(m):
        try:
            data = json.loads(base64.b64decode(m.group(2)))
        except Exception:  # noqa: BLE001
            return m.group(0)
        if isinstance(data, dict):
            data.pop("at", None)
        return f"<{m.group(1)} " + json.dumps(data, sort_keys=True) + ">"

    return HUMAN.sub("TIME", TS.sub("T", MARK.sub(dec, text or "")))


def walk(x):
    if isinstance(x, str):
        return norm(x)
    if isinstance(x, dict):
        return {str(k): walk(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        items = [walk(v) for v in x]
        return sorted(items, key=repr) if isinstance(x, set) else items
    if isinstance(x, (int, float, bool)) or x is None:
        return x
    return norm(str(x))


SKIP = {"calls", "_next"}


def snapshot(gh):
    return walk({k: v for k, v in vars(gh).items() if k not in SKIP and not callable(v)})


def main(scenarios, run):
    outp = sys.argv[2]
    shard = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    flt = os.environ.get("FILTER")
    with gzip.open(outp, "wt", compresslevel=3) as f:
        for i, sc in enumerate(scenarios()):
            if i % n != shard:
                continue
            if flt and not eval(flt, {}, dict(sc)):
                continue
            f.write(json.dumps(run(sc), sort_keys=True, default=str) + "\n")
