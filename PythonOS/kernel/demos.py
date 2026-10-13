from satanic import print_satanic, int_to_str, bool_to_str

def fib(n: int) -> int:
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)

def string_ops_demo():
    s = "Hello, PythonOS!"
    print_satanic(s)
    print_satanic(int_to_str(len(s)))
    print_satanic(s[0])
    print_satanic(s[-1])
    print_satanic(s[7:15])
    print_satanic(s[:5])
    print_satanic(s[7:])
    print_satanic(bool_to_str(s == "Hello, PythonOS!"))
    print_satanic(bool_to_str(s == "nope"))
    print_satanic(bool_to_str("abc" < "abd"))
    print_satanic("-" * 20)
    print_satanic(3 * "ab")
    print_satanic(s[::-1])
    print_satanic(s[::2])
    print_satanic(s[::-2])

class Point:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y
    def total(self):
        return self.x + self.y
    def move(self, dx: int, dy: int):
        self.x = self.x + dx
        self.y = self.y + dy
    def __eq__(self, other):
        return self.x == other.x and self.y == other.y
    def __lt__(self, other):
        return self.total() < other.total()

def classes_demo():
    p = Point(3, 4)
    print_satanic(int_to_str(p.x))
    print_satanic(int_to_str(p.y))
    print_satanic(int_to_str(p.total()))
    p.move(2, 3)
    print_satanic(int_to_str(p.x))
    print_satanic(int_to_str(p.y))
    print_satanic(int_to_str(p.total()))
    q = Point(5, 7)
    r = Point(5, 7)
    print_satanic(bool_to_str(p == q))
    print_satanic(bool_to_str(q == r))
    print_satanic(bool_to_str(q != r))
    print_satanic(bool_to_str(p < q))
    print_satanic(bool_to_str(q < p))
