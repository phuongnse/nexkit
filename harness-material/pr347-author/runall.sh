#!/bin/bash
# usage: runall.sh HARNESS TAG SHARDS -> out/TAG/{old,new}.N.gz ; SIDES="old new" by default
C=$SCRATCH/d312
OLD=$SCRATCH/r337/main
H=$1; T=$2; N=${3:-8}; NEW=${NEW:-$C/new}
mkdir -p $C/out/$T
for side in ${SIDES:-old new}; do
  root=$OLD; [ $side = new ] && root=$NEW
  for i in $(seq 0 $((N-1))); do
    PYTHONHASHSEED=0 python3 $C/$H $root $C/out/$T/$side.$i $i $N 2> $C/out/$T/$side.$i.err &
  done
done
wait
