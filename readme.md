# python-kernel

A small experimental Python-based kernel and compiler/runtime project that demonstrates running Python-like programs in a kernel environment and in freestanding/hosted runtimes.

This repository combines:
- a compiler toolchain in `pyck/`
- a kernel implementation in `PythonOS/`
- runtime code in `runtime/`
- example programs in `examples/`

## Overview

The project is split into a few major parts:

- `pyck/` — compiler frontend and code generation pipeline
- `PythonOS/` — a toy operating system/kernel implementation in Python
- `runtime/` — C++ runtime support for hosted and freestanding execution
- `examples/` — build/run scripts and sample programs
- `tests/` — project tests

## Repository structure

```
PythonOS/
  build.py
  kernel/
    acpi.py
    cpio.py
    cpu.py
    demos.py
    elf.py
    gdt.py
    graphics.py
    idt.py
    ioapic.py
    lapic.py
    main.py
    memory.py
    pci.py
    pit.py
    process.py
    ps2.py
    satanic.py
    syscall.py
    threads.py
    usb_hid.py
    vmm.py
    xhci.py

pyck/
  __init__.py
  __main__.py
  ast_to_ir.py
  codegen.py
  escape.py
  imports.py
  ir.py
  typechecker.py
  types.py

runtime/
  freestanding/
    py_runtime.cpp
    pyck_runtime.h
  hosted/
    py_print.cpp

apps/
  child/
  hello/

examples/
  fibonacci.py
  hello.py
  kernel_hello.py
  print42.py
  run_fibonacci.sh
  run_hello.sh
  run_kernel_hello.sh
  run_kernel_hello_x86_64.sh
  run_print42.sh

arch/
tests/
.gitignore
```

## What the compiler does

The `pyck` package is the compiler front-end. It contains:

- `ast_to_ir.py` — converts Python AST into an internal IR
- `ir.py` — IR definitions and helpers
- `typechecker.py` — type checking and validation
- `codegen.py` — emits executable code for the runtime
- `imports.py`, `escape.py`, `types.py` — supporting compiler infrastructure

## What the kernel does

`PythonOS/kernel/` contains an experimental kernel implementation written in Python. It includes support for:

- CPU and interrupt handling
- memory management
- process/thread management
- PCI and USB HID handling
- APIC / IOAPIC / PIT / PS/2 support
- graphics and syscall infrastructure
- ELF loading and runtime features

`PythonOS/build.py` is the project’s build entry point for assembling kernel artifacts.

## Runtime

The runtime is split between:
- `runtime/freestanding/` — C++ runtime used for freestanding/bare-metal execution
- `runtime/hosted/` — minimal hosted runtime utilities

## Examples

The repository includes example programs under `examples/` and shell scripts to run them:
- `hello.py`
- `fibonacci.py`
- `kernel_hello.py`
- `print42.py`
- `run_hello.sh`
- `run_fibonacci.sh`
- `run_kernel_hello.sh`
- `run_kernel_hello_x86_64.sh`
- `run_print42.sh`

## Prerequisites

You will likely need:
- Python 3
- A C++ toolchain
- An emulator or suitable environment for kernel examples (for example QEMU)

## Quick start

```bash
git clone https://github.com/nekoflare/python-kernel.git
cd python-kernel
python -m pyck
python PythonOS/build.py
bash examples/run_hello.sh
```

The exact behavior depends on the target platform and script configuration, so the scripts in `examples/` are the best reference.

## Running tests

```bash
python -m pytest tests
```

## License

This repository does not currently include a `LICENSE` file in the root directory. Before reusing or redistributing this project, verify the licensing terms with the repository owner.

## Contributing

Contributions are welcome. If you want to improve the compiler, kernel, runtime, docs, or examples:
- open a focused pull request
- document any runtime or ABI changes
- keep examples and scripts consistent with the current build flow
