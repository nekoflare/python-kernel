#!/bin/bash
# Run the fibonacci example in hosted mode
cd "$(dirname "$0")/.."
python3 -m pyck examples/fibonacci.py --target=hosted --max-recursion-depth=1000000 -o /tmp/pyck_fib && /tmp/pyck_fib
