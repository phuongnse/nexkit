#!/bin/bash
C=$SCRATCH/d312
cd $C
for seed in $(seq 1 16); do
  timeout 3000 python3 fuzz_engine.py $C/new 1100 $((seed+200)) > out/fuzz/$seed.txt 2>&1 &
done
wait
for f in out/fuzz/*.txt; do sed -n 2p $f; done | sort | uniq -c > out/fuzz/summary.txt
