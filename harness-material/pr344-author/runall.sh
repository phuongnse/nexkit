#!/bin/bash
# usage: runall.sh HARNESS TAG SHARDS -> out/TAG/{old,new}.N.gz
C=$SCRATCH/c342
H=$1; T=$2; N=${3:-8}; NEW=${NEW:-$HOME/repos/nexkit-342}
mkdir -p $C/out/$T
for side in old new; do
  root=$C/old; [ $side = new ] && root=$NEW
  for i in $(seq 0 $((N-1))); do
    PYTHONHASHSEED=0 python3 $C/$H $root $C/out/$T/$side.$i $i $N 2> $C/out/$T/$side.$i.err &
  done
done
wait
