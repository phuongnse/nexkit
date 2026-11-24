#!/bin/bash
# Run every harness old vs new and compare; results in out/summary.txt
C=$SCRATCH/d312
cd $C
: > out/summary.txt
for pair in ${HARNESSES:-hdiag:diag hconf:conf hrev:rev hsweep:sweep hround:round hreq:req hgive:give hstop:stop hreport:report}; do
  h=${pair%%:*}; tag=${pair##*:}
  rm -rf out/$tag
  COUNT=60000 ./runall.sh $h.py $tag 8
  echo "== $h" >> out/summary.txt
  python3 compare.py out/$tag/old out/$tag/new 8 >> out/summary.txt 2>&1
  cat out/$tag/*.err | sort | uniq -c | head -5 >> out/summary.txt
done
echo DONE >> out/summary.txt
