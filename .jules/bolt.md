## 2024-05-18 - [Optimized Iterative String Concatenation]
**Learning:** In Python, doing `text += block.get("text", "")` repeatedly inside a loop takes O(N^2) time since strings are immutable and each addition copies the string.
**Action:** When accumulating strings in a loop, it is more performant to collect the string pieces using a generator or list comprehension and combine them using `"".join()`, reducing the time complexity to O(N).
