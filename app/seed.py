"""Deterministic fake data for the sample database."""
import random
from datetime import date, timedelta

DEPARTMENTS = ["Sales", "Support", "Engineering", "Marketing", "Finance"]
FIRST = ["Aarav", "Maya", "Liam", "Sofia", "Noah", "Emma", "Ethan", "Olivia", "Lucas", "Ava",
         "Mason", "Isla", "Arjun", "Zoe", "Kabir", "Nina", "Owen", "Priya", "Jack", "Lena"]
LAST = ["Smith", "Patel", "Garcia", "Chen", "Johnson", "Brown", "Lee", "Davis", "Martin", "Kumar",
        "Wilson", "Lopez", "Clark", "Young", "Singh", "Baker"]
LOCATIONS = [("Los Angeles", "California"), ("San Francisco", "California"), ("San Diego", "California"),
             ("Austin", "Texas"), ("Houston", "Texas"), ("New York", "New York"), ("Buffalo", "New York"),
             ("Miami", "Florida"), ("Seattle", "Washington"), ("Chicago", "Illinois"),
             ("Denver", "Colorado"), ("Boston", "Massachusetts")]
PRODUCTS = [
    ("Laptop Pro 14", "Electronics", 1499.0), ("Laptop Air 13", "Electronics", 1099.0),
    ("Wireless Mouse", "Accessories", 29.99), ("Mechanical Keyboard", "Accessories", 89.0),
    ("USB-C Hub", "Accessories", 49.5), ("4K Monitor 27in", "Electronics", 399.0),
    ("Noise-Cancelling Headphones", "Electronics", 249.0), ("Webcam HD", "Electronics", 69.0),
    ("Standing Desk", "Furniture", 549.0), ("Ergonomic Chair", "Furniture", 329.0),
    ("Bookshelf", "Furniture", 139.0), ("Desk Lamp", "Furniture", 39.0),
    ("Notebook Pack", "Office", 12.5), ("Gel Pens (12)", "Office", 9.99),
    ("Whiteboard", "Office", 79.0), ("Printer Paper (500)", "Office", 7.49),
    ("Laptop Sleeve", "Accessories", 24.0), ("Phone Stand", "Accessories", 15.0),
    ("Tablet 10in", "Electronics", 329.0), ("Smart Speaker", "Electronics", 59.0),
]
STATUSES = ["Pending", "Shipped", "Delivered", "Delivered", "Delivered", "Cancelled"]


def _d(rng, start, end):
    return start + timedelta(days=rng.randint(0, (end - start).days))


def seed(conn):
    rng = random.Random(42)
    cur = conn.cursor()

    # Employees: first 3 are managers; ~7 hired on/after 2024-01-01
    employees = []
    for i in range(1, 26):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        dept = DEPARTMENTS[(i - 1) % len(DEPARTMENTS)]
        hire = _d(rng, date(2024, 1, 5), date(2025, 9, 30)) if i > 18 else _d(rng, date(2018, 1, 1), date(2023, 12, 20))
        salary = round(rng.uniform(45000, 140000), 2)
        manager = None if i <= 3 else rng.randint(1, 3)
        employees.append((i, name, dept, hire.isoformat(), salary, manager))
    cur.executemany("INSERT INTO Employees VALUES (?,?,?,?,?,?)", employees)

    customers = []
    for i in range(1, 61):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        city, state = LOCATIONS[(i * 7 + rng.randint(0, 3)) % len(LOCATIONS)]
        if i % 5 == 0:
            city, state = rng.choice(LOCATIONS[:3])  # make California well represented
        customers.append((i, f"{first} {last}", f"{first}.{last}{i}@example.com".lower(), city, state,
                          _d(rng, date(2022, 1, 1), date(2025, 8, 1)).isoformat()))
    cur.executemany("INSERT INTO Customers VALUES (?,?,?,?,?,?)", customers)

    cur.executemany("INSERT INTO Products VALUES (?,?,?,?)",
                    [(i + 1, n, c, p) for i, (n, c, p) in enumerate(PRODUCTS)])

    sales_ids = [e[0] for e in employees if e[2] == "Sales"]
    orders, items, item_id = [], [], 1
    for oid in range(1, 201):
        orders.append((oid, rng.randint(1, 60), rng.choice(sales_ids),
                       _d(rng, date(2024, 1, 1), date(2025, 10, 31)).isoformat(), rng.choice(STATUSES)))
        for pid in rng.sample(range(1, len(PRODUCTS) + 1), rng.randint(1, 3)):
            items.append((item_id, oid, pid, rng.randint(1, 4), PRODUCTS[pid - 1][2]))
            item_id += 1
    cur.executemany("INSERT INTO Orders VALUES (?,?,?,?,?)", orders)
    cur.executemany("INSERT INTO OrderItems VALUES (?,?,?,?,?)", items)
    conn.commit()
