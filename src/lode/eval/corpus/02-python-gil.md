# The Python GIL and concurrency choices

CPython has a Global Interpreter Lock, so only one thread executes Python
bytecode at a time. Threads are therefore useful for I/O-bound work, where they
overlap waiting on the network or disk, but they do not speed up CPU-bound pure
Python code.

For CPU-bound work the standard answer is multiprocessing, which runs separate
interpreter processes each with its own GIL, at the cost of inter-process
communication overhead. Libraries like NumPy sidestep the issue differently:
their heavy loops release the GIL while running in C.

Python 3.13 ships an experimental free-threaded build that can disable the GIL,
but it is opt-in and not yet the default. For now, the practical rule stays the
same: threads for I/O, processes for CPU.
