#!/bin/bash
# run.sh <scenarios> <tag>: play on both checkouts in parallel chunks (hashes only), then compare.
S=$1; T=$2; D=$(dirname $(readlink -f $0)); V6=${V6REPO:-$SCRATCH/r337/main}; PR=${PRREPO:-$HOME/repos/nexkit-r347}
split -n l/${JOBS:-6} -d $S $D/chunk_${T}_
for c in $D/chunk_${T}_??; do
  python3 $D/drive.py $PR $c > $c.pr &
  python3 $D/drive.py $V6 $c > $c.v6 &
done; wait
cat $D/chunk_${T}_??.pr > $D/h_${T}_pr.jsonl; cat $D/chunk_${T}_??.v6 > $D/h_${T}_v6.jsonl
rm -f $D/chunk_${T}_*
python3 - $D/h_${T}_pr.jsonl $D/h_${T}_v6.jsonl <<'PY'
import json, sys, collections
a={json.loads(l)["index"]: json.loads(l) for l in open(sys.argv[1])}
b={json.loads(l)["index"]: json.loads(l) for l in open(sys.argv[2])}
diff=[i for i in a if a[i]["h"] != b.get(i, {}).get("h")]
fatal=[i for i in a if a[i].get("fatal")]
crash=collections.Counter(c for i in a for (_,_,c) in a[i]["brief"] if c)
steps=sum(len(a[i]["brief"]) for i in a)
print("scenarios", len(a), "rounds", steps, "fatal", len(fatal), "differ", len(diff), "crashes(PR)", dict(crash))
print("DIFF", ",".join(map(str, diff[:200])))
PY
