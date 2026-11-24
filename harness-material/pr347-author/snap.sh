#!/bin/bash
# snapshot the branch's code into new/
C=$SCRATCH/d312
rm -rf $C/new && mkdir -p $C/new && cd ~/repos/nexkit-312d && cp -r nexkit tests docs $C/new/ && find $C/new -name __pycache__ -prune -exec rm -rf {} +
