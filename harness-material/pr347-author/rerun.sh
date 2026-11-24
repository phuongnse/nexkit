#!/bin/bash
C=$SCRATCH/d312
cd $C
rm -f out/conf/new.*; SIDES=new ./runall.sh hconf.py conf 8
python3 compare.py out/conf/old out/conf/new 8 > out/conf2.txt
rm -rf out/mut_m2; mkdir -p out/mut_m2; for i in $(seq 0 7); do ln -s $C/out/conf/old.$i out/mut_m2/old.$i; done
SIDES=new NEW=$C/mut/m2 ./runall.sh hconf.py mut_m2 8
python3 compare.py out/mut_m2/old out/mut_m2/new 8 | head -4 >> out/conf2.txt
echo DONE >> out/conf2.txt
