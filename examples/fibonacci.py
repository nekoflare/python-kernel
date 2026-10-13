"""Fibonacci example for PyCk."""
def fibonacci(n: int) -> int:
    if n <= 1:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)

def main():
    i = 0
    while i < 20:
        result = fibonacci(i)
        print(result)
        i = i + 1

main()
