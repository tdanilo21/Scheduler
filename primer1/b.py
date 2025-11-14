import random
import time

time.sleep(10)
with open('data/b.txt', 'w') as f:
    n = random.randint(1000, 10000)
    for _ in range(n):
        f.write(f"{random.randint(1, 1000)} ")
    f.write("\n")