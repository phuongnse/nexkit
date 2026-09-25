#!/bin/sh
# Native CLI metadata discovery runs outside its tool sandbox. Disable that
# optional Git integration; sandboxed terminal tools receive the real Git PATH.
exit 127
