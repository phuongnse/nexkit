#!/bin/bash
# Full suite on each mutant; one line per mutant: name, failures/errors.
cd $1 && out=$(python3 -m unittest discover -s tests -t . 2>&1 | tail -3 | tr '\n' ' ')
echo "$(basename $1): $out"
