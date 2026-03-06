import time

class MockCell:
    def __init__(self, value):
        self.value = value
    def getString(self):
        time.sleep(0.0001) # Simulate slow UNO call
        return str(self.value)

class MockRows:
    def __init__(self, count):
        self.count = count
    def getCount(self):
        return self.count

class MockColumns:
    def __init__(self, count):
        self.count = count
    def getCount(self):
        return self.count

class MockTextTable:
    def __init__(self, rows, cols):
        self.rows = rows
        self.cols = cols
        self._data = [["Value %d,%d" % (r, c) for c in range(cols)] for r in range(rows)]

    def getRows(self):
        return MockRows(self.rows)

    def getColumns(self):
        return MockColumns(self.cols)

    def getCellByName(self, name):
        # Extremely simplified, just returns a mock cell
        return MockCell("Mock Value")

    def getDataArray(self):
        # Mocking getDataArray which is fast
        return tuple(tuple(row) for row in self._data)

def _col_letter(c):
    if c < 26:
        return chr(ord("A") + c)
    return "A" + chr(ord("A") + c - 26)

def read_baseline(table, rows, cols):
    data = []
    for r in range(rows):
        row_data = []
        for c in range(cols):
            col_letter = _col_letter(c)
            cell_ref = "%s%d" % (col_letter, r + 1)
            try:
                row_data.append(table.getCellByName(cell_ref).getString())
            except Exception:
                row_data.append("")
        data.append(row_data)
    return data

def read_optimized(table):
    data_array = table.getDataArray()
    data = []
    for row in data_array:
        row_data = []
        for cell in row:
            row_data.append(str(cell) if cell is not None else "")
        data.append(row_data)
    return data

if __name__ == "__main__":
    table = MockTextTable(100, 10) # 100 rows, 10 columns (1000 cells)
    rows = table.getRows().getCount()
    cols = table.getColumns().getCount()

    start_time = time.time()
    read_baseline(table, rows, cols)
    baseline_time = time.time() - start_time
    print(f"Baseline (cell-by-cell): {baseline_time:.4f} seconds")

    start_time = time.time()
    read_optimized(table)
    optimized_time = time.time() - start_time
    print(f"Optimized (getDataArray): {optimized_time:.4f} seconds")

    if optimized_time > 0:
        print(f"Speedup: {baseline_time / optimized_time:.2f}x")
