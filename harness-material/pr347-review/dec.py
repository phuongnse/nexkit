import base64, json, re, sys
def decode(text):
    def rep(m):
        try:
            return "<STATE " + json.dumps(json.loads(base64.b64decode(m.group(2))), sort_keys=True) + ">"
        except Exception:
            return m.group(0)
    return re.sub(r"<!-- nexkit:(state|run) (\S+) -->", rep, text)
def norm(obj):
    if isinstance(obj, str):
        return decode(obj) if "nexkit:" in obj else obj
    if isinstance(obj, list):
        return [norm(x) for x in obj]
    if isinstance(obj, dict):
        return {k: norm(v) for k, v in obj.items()}
    return obj
if __name__ == "__main__":
    for line in sys.stdin:
        print(json.dumps(norm(json.loads(line)), sort_keys=True))
