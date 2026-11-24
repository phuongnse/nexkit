#!/bin/bash
C=$SCRATCH/d312
cd $C
: > out/mut.txt
for spec in m1:hdiag:diag m3:hdiag:diag m2:hconf:conf; do
  IFS=: read m h tag <<< "$spec"
  t=mut_$m
  rm -rf out/$t; mkdir -p out/$t
  for i in $(seq 0 7); do ln -s $C/out/$tag/old.$i out/$t/old.$i; done
  SIDES=new NEW=$C/mut/$m COUNT=60000 ./runall.sh $h.py $t 8
  echo "== $m $h" >> out/mut.txt
  python3 compare.py out/$t/old out/$t/new 8 | head -3 >> out/mut.txt
done
echo DONE >> out/mut.txt
