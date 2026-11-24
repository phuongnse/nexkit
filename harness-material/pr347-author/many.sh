#!/bin/bash
C=$SCRATCH/d312
cd $C; rm -rf out/diagmany
MANY=1 COUNT=20000 ./runall.sh hdiag.py diagmany 8
python3 compare.py out/diagmany/old out/diagmany/new 8 > out/many.txt 2>&1
echo DONE >> out/many.txt
