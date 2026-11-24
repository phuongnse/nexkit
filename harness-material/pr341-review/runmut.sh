#!/bin/bash
# usage: runmut.sh NAME
S=$SCRATCH/r341
N=$1; D=$S/mut/$N
rm -rf $D; mkdir -p $D
git -C $HOME/repos/nexkit-r341 archive HEAD | tar -x -C $D
python3 - "$N" "$D" <<'PY'
import runpy, sys
M = runpy.run_path('$SCRATCH/r341/mutants.py')['M']
f,a,b = M[sys.argv[1]]
p = sys.argv[2] + '/' + f
s = open(p).read(); assert s.count(a) == 1; open(p, 'w').write(s.replace(a, b))
PY
cd $D && timeout 900 python3 -m unittest discover -s tests -t . > $D.log 2>&1
echo "$N exit=$? $(tail -1 $D.log) $(grep -E '^(FAIL|ERROR):' $D.log | head -5 | tr '\n' ';')" >> $S/mut/results.txt
