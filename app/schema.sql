-- Sample company database (SQLite). The agent may only query these tables.
-- Dates are stored as ISO-8601 text: 'YYYY-MM-DD'.

CREATE TABLE Employees (
    EmployeeID  INTEGER PRIMARY KEY,
    Name        TEXT NOT NULL,
    Department  TEXT NOT NULL,          -- Sales, Support, Engineering, Marketing, Finance
    HireDate    TEXT NOT NULL,
    Salary      REAL NOT NULL,
    ManagerID   INTEGER,
    FOREIGN KEY (ManagerID) REFERENCES Employees(EmployeeID)
);

CREATE TABLE Customers (
    CustomerID  INTEGER PRIMARY KEY,
    Name        TEXT NOT NULL,
    Email       TEXT NOT NULL,
    City        TEXT,
    State       TEXT,                   -- full state name, e.g. 'California'
    CreatedAt   TEXT NOT NULL
);

CREATE TABLE Products (
    ProductID   INTEGER PRIMARY KEY,
    Name        TEXT NOT NULL,
    Category    TEXT NOT NULL,          -- Electronics, Furniture, Office, Accessories
    Price       REAL NOT NULL
);

CREATE TABLE Orders (
    OrderID     INTEGER PRIMARY KEY,
    CustomerID  INTEGER NOT NULL,
    EmployeeID  INTEGER,                -- the sales employee who handled the order
    OrderDate   TEXT NOT NULL,
    Status      TEXT NOT NULL,          -- Pending, Shipped, Delivered, Cancelled
    FOREIGN KEY (CustomerID) REFERENCES Customers(CustomerID),
    FOREIGN KEY (EmployeeID) REFERENCES Employees(EmployeeID)
);

CREATE TABLE OrderItems (
    OrderItemID INTEGER PRIMARY KEY,
    OrderID     INTEGER NOT NULL,
    ProductID   INTEGER NOT NULL,
    Quantity    INTEGER NOT NULL,
    UnitPrice   REAL NOT NULL,
    FOREIGN KEY (OrderID)   REFERENCES Orders(OrderID),
    FOREIGN KEY (ProductID) REFERENCES Products(ProductID)
);
