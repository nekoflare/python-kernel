#!/bin/bash
# Run the hello world example in hosted mode
cd "$(dirname "$0")/.."
python3 -m pyck examples/hello.py --target=hosted -o /tmp/pyck_hello && /tmp/pyck_hello
